"""
新疆燃气接口访问层。
负责与远程服务完成加密握手、登录认证与数据查询。
该模块封装了请求头构造、会话维护、加解密以及重试机制，供集成与调试脚本复用。
"""
import logging
import json
import time
import threading
from typing import Dict, Any, Optional
import requests
from requests.adapters import HTTPAdapter
from datetime import datetime, timedelta
from .crypto import GasCrypto
try:
    from homeassistant.util import dt as dt_util
except ModuleNotFoundError:
    class _DateUtil:
        @staticmethod
        def now():
            return datetime.now()
    dt_util = _DateUtil()

_LOGGER = logging.getLogger(__name__)

class XjGasAPI:
    """
    新疆燃气 API 接口类。
    负责处理与新疆燃气服务器的通信，包括加密握手、登录认证以及各项数据的获取。
    
    安全机制说明：
    该接口采用混合加密机制（RSA + AES）来确保通信安全：
    1. RSA-1024: 用于在握手阶段安全地交换 AES 密钥。客户端生成自己的 RSA 密钥对，
       并将公钥发送给服务器，服务器使用该公钥加密 AES 密钥返回给客户端。
    2. AES-128-ECB: 用于后续所有业务数据的加解密。业务数据（如登录凭证、查询参数）
       使用 AES 密钥加密后传输，服务器返回的数据也是 AES 加密的。
    """
    
    def __init__(self, phone: str = None, password: str = None):
        """
        初始化 API 实例。
        
        Args:
            phone (str): 用户的手机号，作为登录账号。
            password (str): 用户的登录密码。
        """
        self._phone = phone
        self._password = password
        self._token = None # 登录成功后获取的会话 Token
        self._openid = None # 用户在微信端的唯一标识 (OpenID 或 UserId)
        
        self._session = requests.Session()
        # 配置连接池和重试策略
        adapter = HTTPAdapter(
            pool_connections=10,  # 增加连接池大小
            pool_maxsize=10,
            max_retries=3  # 添加重试机制，自动处理连接错误
        )
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)
        
        # 禁用 urllib3 的 SSL 警告
        # 因为服务器证书可能存在问题或我们选择忽略证书验证，所以需要禁用警告以保持日志清洁
        requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)
        self._session.verify = False # 全局禁用 SSL 验证，避免自签名证书报错
        
        # API 基础 URL
        self._api_base = "https://wgas.xjrq.net/api"
        self._base_headers = {
            'Host': 'wgas.xjrq.net',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf254171e) XWEB/18787 Flue',
            'Content-Type': 'application/json; charset=UTF-8',
            'Origin': 'https://wgas.xjrq.net',
            'Referer': 'https://wgas.xjrq.net/?state=7',
            'part': 'WECHAT',
            'fromDomainId': '7'
        }
        
        # 加密相关的密钥存储变量
        self._crypto = GasCrypto() # 独立的加密模块实例
        self._busy_token = ""             # 用户会话 busyToken，用于维持会话状态
        self._cons_no = None              # 用户户号 (Contract Number)，查询账单等需要
        self._login_failed_at = None      # 记录上次登录失败的时间戳，用于冷却控制
        self._request_lock = threading.RLock() # 线程锁，确保并发请求时的线程安全
        
    @property
    def cons_no(self):
        """获取用户户号"""
        return self._cons_no

    def _build_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """
        构造请求头。
        
        基础请求头用于模拟微信端访问，extra 用于附加认证信息或动态字段。
        
        Args:
            extra (dict, optional): 需要合并到基础 Headers 中的额外键值对。
            
        Returns:
            dict: 完整的请求头字典。
        """
        if extra:
            return {**self._base_headers, **extra}
        return dict(self._base_headers)

    def _handshake(self) -> bool:
        """
        与服务器进行安全握手，增强异常处理和网络错误处理。
        
        握手流程：
        1. 客户端生成 RSA 密钥对。
        2. 客户端将 RSA 公钥发送给服务器接口 `/sec/serverKey`。
        3. 服务器返回其 RSA 公钥 (serverKey) 和可能的其他会话标识 (secret, busyToken)。
        
        Returns:
            bool: 握手是否成功。如果已有服务端公钥，则直接返回 True。
        """
        # 如果已经获取了服务端公钥，说明握手已完成，直接返回成功
        if self._crypto.has_server_key:
            return True
            
        try:
            # 获取清理后的客户端公钥
            client_pub_clean = self._crypto.get_client_public_key_clean()
            
            url = f'{self._api_base}/sec/serverKey'
            
            # 构造请求头，模拟微信小程序或浏览器的行为
            # 这些特定的 Header 对于绕过反爬虫机制可能很重要
            headers = self._build_headers()
            
            # 发送 POST 请求，载荷包含清理后的客户端公钥
            # data 字段为空字符串，这是旧版协议的残留，保持兼容性
            response = self._session.post(url, json={"key": client_pub_clean, "data": ""}, headers=headers, timeout=10, verify=False)
            
            # 检查HTTP响应状态
            if response.status_code != 200:
                _LOGGER.error("握手请求失败，HTTP状态码：%d，URL：%s", response.status_code, url)
                return False
                
            # 解析JSON响应
            try:
                resp_json = response.json()
            except json.JSONDecodeError as json_ex:
                _LOGGER.error("握手响应JSON解析失败：%s，响应内容：%s", json_ex, response.text[:200])
                return False
            
            # 兼容两种可能的 API 返回结构：
            # 1. 新版结构: code=0, obj={serverKey, secret, busyToken}
            # 2. 旧版结构: success=true, obj={key, data}
            
            if resp_json.get('code') == '0' or resp_json.get('success'):
                obj = resp_json.get('obj', {})
                
                # 尝试提取 serverKey (服务端公钥)
                # 某些版本可能返回在 data 字段中
                server_key = obj.get('serverKey') or obj.get('data')
                
                # 尝试提取 secret (有时也叫 key)
                self._crypto.secret = obj.get('secret') or obj.get('key')
                
                # 提取会话 Token
                self._busy_token = obj.get('busyToken')
                
                if server_key:
                    if self._crypto.set_server_public_key(server_key):
                        _LOGGER.debug("握手成功，已获取并导入服务器公钥")
                        return True
                    else:
                        _LOGGER.error("导入服务器公钥失败")
                        return False
                else:
                    _LOGGER.error("握手响应中缺少serverKey: %s", resp_json)
                    return False
            
            _LOGGER.error("握手失败，服务器返回错误: %s", resp_json)
            return False
            
        except requests.exceptions.Timeout as timeout_ex:
            _LOGGER.error("握手请求超时: %s, URL: %s", timeout_ex, url)
            return False
        except requests.exceptions.ConnectionError as conn_ex:
            _LOGGER.error("握手网络连接错误: %s, URL: %s", conn_ex, url)
            return False
        except requests.exceptions.RequestException as req_ex:
            _LOGGER.error("握手请求异常: %s, URL: %s", req_ex, url)
            return False
        except Exception as e:
            _LOGGER.error("握手过程发生未知异常: %s (类型: %s)", e, type(e).__name__)
            return False

    def _encrypt_payload(self, data_dict: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """
        加密请求载荷 (核心加密逻辑)。
        
        Args:
            data_dict (dict): 原始业务数据。
            
        Returns:
            Optional[Dict[str, str]]: 加密后的载荷，包含 'data' 和 'key' 字段。
        """
        return self._crypto.encrypt_payload(data_dict)

    def _decrypt_payload(self, encrypted_resp: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """
        解密响应载荷。
        
        Args:
            encrypted_resp (dict): 加密的响应数据，包含 'data' 和 'key' 字段。
            
        Returns:
            Optional[Dict[str, Any]]: 解密后的原始业务数据字典。
        """
        return self._crypto.decrypt_payload(encrypted_resp)

    def login(self) -> bool:
        """
        执行用户登录操作，增强异常处理和数据验证。
        
        使用手机号和密码进行认证，成功后获取 Token 和 OpenID。
        登录失败会记录时间戳，短时间内避免重复尝试。
        
        流程：
        1. 检查冷却时间，避免频繁失败请求。
        2. 确保已握手（拥有加密通道）。
        3. 构造包含手机号和密码的载荷并加密。
        4. 发送登录请求。
        5. 解密响应，提取 Token, OpenID 和 户号(ConsNo)。
        
        Returns:
            bool: 登录是否成功。
        """
        with self._request_lock:
            # 登录失败后 60 秒内不重复尝试，避免触发风控
            if self._login_failed_at and time.monotonic() - self._login_failed_at < 60:
                _LOGGER.debug("登录冷却中，跳过本次登录尝试")
                return False

            if not self._handshake():
                self._login_failed_at = time.monotonic()
                return False
                
            # 验证输入参数
            if not self._phone or not self._password:
                _LOGGER.error("登录失败：手机号或密码为空")
                self._login_failed_at = time.monotonic()
                return False
                
            # 构造登录载荷
            # wechatProId 固定为 1，可能是区分客户端类型的标识
            payload = {
                "mobile": str(self._phone),
                "password": str(self._password),
                "wechatProId": 1
            }
            
            # 加密登录请求
            encrypted_payload = self._encrypt_payload(payload)
            if not encrypted_payload:
                _LOGGER.error("登录失败：请求数据加密失败")
                self._login_failed_at = time.monotonic()
                return False
                
            url = f'{self._api_base}/login/doLoginByPwd'
            
            # 完整的 Header 结构，包含握手获取的 secret 和 busyToken
            headers = self._build_headers({
                'secret': self._crypto.secret or '',
                'busyToken': self._busy_token or '',
                'openId': self._openid or ''
            })
            
            try:
                response = self._session.post(url, json=encrypted_payload, headers=headers, timeout=10)
                
                # 检查HTTP响应状态
                if response.status_code != 200:
                    _LOGGER.error("登录请求失败，HTTP状态码: %d, URL: %s", response.status_code, url)
                    self._login_failed_at = time.monotonic()
                    return False
                
                # 解析JSON响应
                try:
                    resp_json = response.json()
                except json.JSONDecodeError as json_ex:
                    _LOGGER.error("登录响应JSON解析失败: %s, 响应内容: %s", json_ex, response.text[:200])
                    self._login_failed_at = time.monotonic()
                    return False
                
                # 检查响应是否包含加密数据 (data 和 key 字段)
                if 'data' in resp_json and 'key' in resp_json:
                    decrypted = self._decrypt_payload(resp_json)
                    if decrypted:
                        _LOGGER.debug("登录响应数据已解密: %s", decrypted)
                        
                        # 检查业务状态码：0 或 200+success=true 表示成功
                        if str(decrypted.get('code')) == '0' or (str(decrypted.get('code')) == '200' and decrypted.get('success')):
                            _LOGGER.info("登录成功")
                            # 提取关键认证信息
                            obj = decrypted.get('obj', {})
                            
                            # 1. 获取 Token：优先从响应头获取，其次从 Body
                            self._token = response.headers.get('token')
                            if not self._token and isinstance(obj, dict):
                                self._token = obj.get('token')
                            
                            # 2. 获取 OpenID/UserId
                            if isinstance(obj, dict):
                                user_info = obj.get('user', {})
                                # 将 ID 转换为字符串作为 openId 使用
                                if user_info.get('id'):
                                    self._openid = str(user_info.get('id'))
                                else:
                                    self._openid = decrypted.get('openId') or decrypted.get('userId')
                                
                                # 3. 提取户号 (ConsNo)
                                # 这是查询具体账单和用气量所必需的
                                self._cons_no = user_info.get('consNo')
                                if not self._cons_no:
                                    # 尝试从默认户号字段获取
                                    default_code = user_info.get('defaultCasCode', {})
                                    if default_code:
                                        self._cons_no = default_code.get('consNo')
                            
                            self._login_failed_at = None
                            return True
                        else:
                            error_msg = decrypted.get('msg', '未知错误')
                            _LOGGER.error("登录业务失败: %s", error_msg)
                            self._login_failed_at = time.monotonic()
                            return False
                
                _LOGGER.error("登录响应异常 (非加密或格式错误): %s", resp_json)
                self._login_failed_at = time.monotonic()
                return False
                
            except requests.exceptions.Timeout as timeout_ex:
                _LOGGER.error("登录请求超时: %s, URL: %s", timeout_ex, url)
                self._login_failed_at = time.monotonic()
                return False
            except requests.exceptions.ConnectionError as conn_ex:
                _LOGGER.error("登录网络连接错误: %s, URL: %s", conn_ex, url)
                self._login_failed_at = time.monotonic()
                return False
            except requests.exceptions.RequestException as req_ex:
                _LOGGER.error("登录请求异常: %s, URL: %s", req_ex, url)
                self._login_failed_at = time.monotonic()
                return False
            except Exception as e:
                _LOGGER.error("登录请求发生未知异常: %s (类型: %s)", e, type(e).__name__)
                self._login_failed_at = time.monotonic()
                return False

    def _authenticated_request(self, path: str, payload: Dict[str, Any], retry: bool = True) -> Optional[Dict[str, Any]]:
        """
        发送需要认证的请求的通用辅助方法，增强异常处理。
        
        功能：
        1. 自动检查 Token，如果不存在则自动登录。
        2. 自动处理请求加密和响应解密。
        3. 处理 Token 过期的情况（自动重试）。
        4. 对网络异常进行一次重试，降低短时抖动影响。
        
        Args:
            path (str): API 路径 (如 '/micro/bill/arrearage')。
            payload (dict): 请求业务参数。
            retry (bool): 失败时是否自动重试 (默认为 True)。
            
        Returns:
            dict: 解密后的响应数据，如果失败则返回 None。
        """
        with self._request_lock:
            # 验证输入参数
            if not path or not isinstance(path, str):
                _LOGGER.error("请求失败：无效的路径参数: %s", path)
                return None
                
            if not isinstance(payload, dict):
                _LOGGER.error("请求失败：无效的载荷参数类型: %s", type(payload).__name__)
                return None
            
            # 如果没有 Token，先尝试登录
            if not self._token:
                if not self.login():
                    return None
            
            # 确保握手状态有效
            if not self._handshake():
                return None
                
            # 加密请求载荷
            encrypted_payload = self._encrypt_payload(payload)
            if not encrypted_payload:
                _LOGGER.error("请求失败：载荷加密失败")
                return None
            
            url = f'{self._api_base}{path}'
            headers = self._build_headers({
                'secret': self._crypto.secret or '',
                'busyToken': self._busy_token or '',
                'token': self._token,
                'openId': self._openid or ''
            })
            
            try:
                response = self._session.post(url, json=encrypted_payload, headers=headers, timeout=10)
                
                # 检查HTTP响应状态
                if response.status_code != 200:
                    _LOGGER.error("请求失败，HTTP状态码: %d, 路径: %s", response.status_code, path)
                    
                    # 如果请求失败，可能是 Token 过期或被服务端踢出
                    if retry:
                        _LOGGER.info(f"请求 {path} 失败 (HTTP {response.status_code})，怀疑 Token 过期，尝试重新登录并重试...")
                        self._token = None # 清除 Token 强制重新登录
                        if self.login():
                            # 递归调用一次，retry=False 防止无限循环
                            return self._authenticated_request(path, payload, retry=False)
                    return None
                
                # 解析JSON响应
                try:
                    resp_json = response.json()
                except json.JSONDecodeError as json_ex:
                    _LOGGER.error("请求响应JSON解析失败: %s, 路径: %s, 响应内容: %s", json_ex, path, response.text[:200])
                    return None
                
                # 检查是否为加密响应并解密
                if 'data' in resp_json and 'key' in resp_json:
                    decrypted = self._decrypt_payload(resp_json)
                    if decrypted:
                        # 使用 DEBUG 级别记录解密后的数据
                        _LOGGER.debug(f"API 响应解密成功 [{path}]: {decrypted}")
                        return decrypted
                    else:
                        _LOGGER.error("响应解密失败，路径: %s", path)
                        
                        # 如果解密失败，可能是 Token 过期或被服务端踢出
                        if retry:
                            _LOGGER.info(f"请求 {path} 解密失败，怀疑 Token 过期，尝试重新登录并重试...")
                            self._token = None # 清除 Token 强制重新登录
                            if self.login():
                                # 递归调用一次，retry=False 防止无限循环
                                return self._authenticated_request(path, payload, retry=False)
                        return None
                
                # 返回原始JSON响应
                return resp_json
                
            except requests.exceptions.Timeout as timeout_ex:
                _LOGGER.error("请求超时: %s, 路径: %s", timeout_ex, path)
                # 网络波动也可能导致失败，尝试重登重试一次
                if retry:
                    _LOGGER.info("请求超时，尝试重新登录并重试...")
                    self._token = None
                    if self.login():
                        return self._authenticated_request(path, payload, retry=False)
                return None
            except requests.exceptions.ConnectionError as conn_ex:
                _LOGGER.error("网络连接错误: %s, 路径: %s", conn_ex, path)
                # 网络波动也可能导致失败，尝试重登重试一次
                if retry:
                    _LOGGER.info("网络连接错误，尝试重新登录并重试...")
                    self._token = None
                    if self.login():
                        return self._authenticated_request(path, payload, retry=False)
                return None
            except requests.exceptions.RequestException as req_ex:
                _LOGGER.error("请求异常: %s, 路径: %s", req_ex, path)
                # 网络波动也可能导致失败，尝试重登重试一次
                if retry:
                    _LOGGER.info("请求异常，尝试重新登录并重试...")
                    self._token = None
                    if self.login():
                        return self._authenticated_request(path, payload, retry=False)
                return None
            except Exception as e:
                _LOGGER.error("请求发生未知异常: %s (类型: %s), 路径: %s", e, type(e).__name__, path)
                # 网络波动也可能导致失败，尝试重登重试一次
                if retry:
                    _LOGGER.info("捕获到异常，尝试重新登录并重试...")
                    self._token = None
                    if self.login():
                        return self._authenticated_request(path, payload, retry=False)
                return None

    def get_arrearage(self) -> Optional[Dict[str, Any]]:
        """
        获取当前欠费和余额信息。
        
        API: /micro/bill/arrearage
        
        Returns:
            dict: 包含欠费和余额信息的字典，例如：
            {
                "balance": "100.00",
                "arrearage": "0.00",
                ...
            }
        """
        if not self._token:
            self.login()
            
        payload = {}
        if self._cons_no:
            payload['consNo'] = self._cons_no
        return self._authenticated_request('/micro/bill/arrearage', payload)

    def _get_date_range(self, months: int = 6) -> tuple:
        """
        辅助方法：获取最近 N 个月的日期范围字符串。
        格式: YYYY-MM-DD
        该范围用于账单与缴费记录的 beginTime/endTime 参数。
        
        Args:
            months (int): 回溯的月数。
            
        Returns:
            tuple: (开始日期字符串, 结束日期字符串)
        """
        end_date = datetime.now()
        start_date = end_date - timedelta(days=30 * months)
        return start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')

    def _get_month_range(self, months: int = 6) -> tuple:
        """
        辅助方法：获取最近 N 个月的月份范围字符串。
        格式: YYYYMM
        该范围用于抄表记录接口的 startYm/endYm 参数。
        
        Args:
            months (int): 回溯的月数。
            
        Returns:
            tuple: (开始月份字符串, 结束月份字符串)
        """
        end_date = datetime.now()
        start_date = end_date - timedelta(days=30 * months)
        return start_date.strftime('%Y%m'), end_date.strftime('%Y%m')

    def get_fee_record(self, page: int = 1, rows: int = 10, months: int = 6) -> Optional[Dict[str, Any]]:
        """
        获取历史账单记录 (月度账单)。
        
        API: /micro/bill/feeRecord
        
        Args:
            page (int): 页码，默认为 1。
            rows (int): 每页条数，默认为 10。
            months (int): 查询最近多少个月的数据，默认为 6。
            
        Returns:
            dict: 账单记录列表数据。
        """
        if not self._token:
            self.login()

        start_time, end_time = self._get_date_range(months)
        payload = {
            "pageNo": page,
            "pageSize": rows,
            "feeType": 0,
            "beginTime": start_time,
            "endTime": end_time
        }
        
        if self._cons_no:
            payload['consNo'] = self._cons_no
        return self._authenticated_request('/micro/bill/feeRecord', payload)

    def get_meter_info(self, months: int = 6) -> Optional[Dict[str, Any]]:
        """
        获取抄表记录 (通常包含月度用气量)。
        
        API: /micro/readMeter/mrInfo
        
        Args:
            months (int): 查询最近多少个月的数据，默认为 6。
            
        Returns:
            dict: 抄表记录数据。
        """
        if not self._token:
            self.login()

        start_ym, end_ym = self._get_month_range(months)
        payload = {
            "pageSize": 24, # 固定页大小，通常够用
            "startYm": start_ym,
            "endYm": end_ym
        }
        if self._cons_no:
            payload['consNo'] = self._cons_no
        return self._authenticated_request('/micro/readMeter/mrInfo', payload)

    def get_payment_record(self, page: int = 1, rows: int = 10, months: int = 6) -> Optional[Dict[str, Any]]:
        """
        获取缴费记录。
        
        API: /micro/bill/payMentRecord
        
        Args:
            page (int): 页码，默认为 1。
            rows (int): 每页条数，默认为 10。
            months (int): 查询最近多少个月的数据，默认为 6。
            
        Returns:
            dict: 缴费记录数据。
        """
        if not self._token:
            self.login()

        start_time, end_time = self._get_date_range(months)
        payload = {
            "pageNo": page,
            "pageSize": rows,
            "beginTime": start_time,
            "endTime": end_time
        }
        if self._cons_no:
            payload['consNo'] = self._cons_no
        return self._authenticated_request('/micro/bill/payMentRecord', payload)

    def get_daily_usage(self, days: int = 30) -> Optional[Dict[str, Any]]:
        """
        获取日用气量数据。
        
        API: /gasVolumeFill/gasDayNum
        注意：此接口需要加密请求。
        参数：startTime, endTime (yyyy-MM-dd)，不包含当天
        
        Args:
            days (int): 查询过去多少天的数据，默认为 30 天。
            
        Returns:
            dict: 日用气量数据。
        """
        if not self._token:
            self.login()

        path = "/gasVolumeFill/gasDayNum"
        
        def _fetch(query_days: int):
            """
            按指定天数构建查询区间并发起请求。
            
            该内部函数复用加密与认证逻辑，返回原始响应数据。
            """
            safe_days = max(1, query_days)
            # 默认取昨天为结束日，确保“前N天且不含当天”
            now_local = dt_util.now()
            end_date = now_local.date()
            start_date = end_date - timedelta(days=safe_days - 1)
            
            payload = {
                 "startTime": start_date.strftime('%Y-%m-%d'), 
                 "endTime": end_date.strftime('%Y-%m-%d'),
                 # 增加 beginTime 兼容不同后端接口定义
                 "beginTime": start_date.strftime('%Y-%m-%d'),
            }
            if self._cons_no:
                payload['consNo'] = self._cons_no
                
            _LOGGER.debug(f"尝试获取日用气量: {path} payload={payload}")
            return self._authenticated_request(path, payload)

        try:
            # 首次尝试 (默认 30 天)
            result = _fetch(days)
            
            # 自动重试逻辑：
            # 如果返回 "400" 错误 (通常是 "时间范围不合法" 或 "范围太大")，
            # 且当前的查询天数大于 7 天，则尝试自动缩小范围为 7 天重试。
            if result and str(result.get('code')) == '400' and days > 30:
                 _LOGGER.warning(f"获取日用气量失败 (范围 {days} 天): {result.get('msg')}，尝试自动缩小范围为 30 天")
                 result = _fetch(30)
                
            if result and (str(result.get('code')) == '200' or result.get('success')):
                return result
            else:
                _LOGGER.warning(f"获取日用气量失败: {result}")
                return None
            
        except Exception as e:
            _LOGGER.error(f"获取日用气量异常: {e}")
            return None
