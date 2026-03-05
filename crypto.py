"""
新疆燃气接口加密解密模块。
负责处理 RSA 密钥生成、公钥交换格式化、AES 加密解密逻辑。
"""
import logging
import json
import base64
import secrets
import string
from typing import Dict, Any, Optional
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad

_LOGGER = logging.getLogger(__name__)

class GasCrypto:
    """
    新疆燃气 API 加密解密工具类。
    
    提供 RSA 非对称加密（用于握手和密钥交换）和 AES 对称加密（用于业务数据传输）的封装。
    """
    def __init__(self):
        """
        初始化加密工具类。
        
        初始化时会自动生成客户端 RSA 密钥对。
        """
        self._client_key_pair = None # 客户端 RSA 密钥对
        self._server_pub_key = None  # 服务端 RSA 公钥
        self._secret = None          # 握手时服务端返回的 Secret 字符串
        self.generate_client_keys()

    @property
    def has_server_key(self) -> bool:
        """检查是否已获取服务端公钥"""
        return self._server_pub_key is not None

    @property
    def secret(self) -> Optional[str]:
        """获取握手返回的 secret"""
        return self._secret
    
    @secret.setter
    def secret(self, value: Optional[str]):
        """设置 secret 值"""
        self._secret = value

    def generate_client_keys(self):
        """
        生成客户端 RSA 密钥对 (1024位)。
        
        使用 PyCryptodome 库生成。
        """
        try:
            self._client_key_pair = RSA.generate(1024)
        except Exception as e:
            _LOGGER.error("生成 RSA 密钥失败：%s", e)

    def get_client_public_key_clean(self) -> str:
        """
        获取清理后的客户端公钥（无头尾、无换行），用于发送给服务器。
        
        服务器接口要求公钥为纯 Base64 字符串，不能包含 PEM 格式的头尾标识。
        
        Returns:
            str: 清理后的公钥字符串。
        """
        if not self._client_key_pair:
            self.generate_client_keys()
            
        client_pub_pem = self._client_key_pair.publickey().export_key().decode('utf-8')
        return client_pub_pem.replace('-----BEGIN PUBLIC KEY-----', '') \
                             .replace('-----END PUBLIC KEY-----', '') \
                             .replace('\n', '')

    def set_server_public_key(self, server_key_str: str) -> bool:
        """
        设置服务端公钥。
        
        如果传入的公钥字符串缺少 PEM 头尾，会自动补充。
        
        Args:
            server_key_str (str): 服务端返回的公钥字符串。
            
        Returns:
            bool: 导入是否成功。
        """
        if not server_key_str:
            return False
            
        try:
            if "-----BEGIN PUBLIC KEY-----" not in server_key_str:
                 pem_key = f"-----BEGIN PUBLIC KEY-----\n{server_key_str}\n-----END PUBLIC KEY-----"
            else:
                 pem_key = server_key_str
                 
            self._server_pub_key = RSA.import_key(pem_key)
            return True
        except Exception as e:
            _LOGGER.error("导入服务器公钥失败: %s", e)
            return False

    def encrypt_payload(self, data_dict: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """
        加密请求载荷 (核心加密逻辑)。
        
        加密流程：
        1. 动态生成一个 16 字节的随机 AES 密钥。
        2. 使用 AES-128-ECB 模式，用生成的密钥加密业务数据 (data_dict)。
        3. 使用服务端 RSA 公钥，加密刚才生成的 AES 密钥。
        
        Args:
            data_dict (dict): 需要加密的原始数据字典。
            
        Returns:
            dict: 包含加密数据(data)和加密后的AES密钥(key)的字典。
                  格式: {"data": "...", "key": "..."}
                  如果失败返回 None。
        """
        if not self._server_pub_key:
            _LOGGER.error("加密失败：缺少服务端公钥，请先握手")
            return None
            
        if not data_dict:
            _LOGGER.error("加密失败：数据字典为空")
            return None
            
        try:
            # 1. 生成 AES 密钥
            chars = string.ascii_letters + string.digits
            aes_key_str = ''.join(secrets.choice(chars) for _ in range(16))
            aes_key_bytes = aes_key_str.encode('utf-8')
            
            # 2. AES 加密业务数据
            cipher_aes = AES.new(aes_key_bytes, AES.MODE_ECB)
            try:
                data_str = json.dumps(data_dict, separators=(',', ':'))
            except (TypeError, ValueError) as json_ex:
                _LOGGER.error("数据JSON序列化失败: %s", json_ex)
                return None
                
            try:
                padded_data = pad(data_str.encode('utf-8'), AES.block_size)
            except ValueError as pad_ex:
                _LOGGER.error("数据填充失败: %s", pad_ex)
                return None
                
            encrypted_data = cipher_aes.encrypt(padded_data)
            encrypted_data_b64 = base64.b64encode(encrypted_data).decode('utf-8')
            
            # 3. RSA 加密 AES 密钥
            cipher_rsa = PKCS1_v1_5.new(self._server_pub_key)
            encrypted_key = cipher_rsa.encrypt(aes_key_bytes)
            encrypted_key_b64 = base64.b64encode(encrypted_key).decode('utf-8')
            
            return {
                "data": encrypted_data_b64,
                "key": encrypted_key_b64
            }
            
        except Exception as e:
            _LOGGER.error("加密过程发生未知错误: %s", e)
            return None

    def decrypt_payload(self, encrypted_resp: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """
        解密响应载荷。
        
        解密流程：
        1. 从响应中提取加密的 AES 密钥 (key) 和加密的数据 (data)。
        2. 使用客户端 RSA 私钥解密 AES 密钥。
        3. 使用解密出的 AES 密钥解密数据内容。
        
        Args:
            encrypted_resp (dict): 包含加密数据(data)和加密密钥(key)的响应字典。
            
        Returns:
            dict: 解密并解析后的 JSON 数据字典，失败返回 None。
        """
        if not self._client_key_pair:
            _LOGGER.error("解密失败：缺少客户端私钥")
            return None
            
        if not encrypted_resp or not isinstance(encrypted_resp, dict):
            _LOGGER.error("解密失败：无效的加密响应数据")
            return None
            
        try:
            encrypted_data_b64 = encrypted_resp.get('data')
            encrypted_key_b64 = encrypted_resp.get('key')
            
            if not encrypted_data_b64 or not isinstance(encrypted_data_b64, str):
                _LOGGER.error("解密失败：缺少或无效的加密数据")
                return None
                
            if not encrypted_key_b64 or not isinstance(encrypted_key_b64, str):
                _LOGGER.error("解密失败：缺少或无效的加密密钥")
                return None
                
            # 1. RSA 解密 AES 密钥
            cipher_rsa = PKCS1_v1_5.new(self._client_key_pair)
            try:
                encrypted_key_bytes = base64.b64decode(encrypted_key_b64)
            except Exception as b64_ex:
                _LOGGER.error("加密密钥Base64解码失败: %s", b64_ex)
                return None
                
            aes_key_bytes = cipher_rsa.decrypt(encrypted_key_bytes, None)
            
            if not aes_key_bytes:
                _LOGGER.error("RSA 解密失败，无法获取 AES 密钥")
                return None
                
            # 2. AES 解密数据
            cipher_aes = AES.new(aes_key_bytes, AES.MODE_ECB)
            try:
                encrypted_data_bytes = base64.b64decode(encrypted_data_b64)
            except Exception as b64_ex:
                _LOGGER.error("加密数据Base64解码失败: %s", b64_ex)
                return None
                
            decrypted_data_bytes = cipher_aes.decrypt(encrypted_data_bytes)
            
            # 3. 去除 PKCS7 填充
            try:
                unpadded_data_bytes = unpad(decrypted_data_bytes, AES.block_size)
            except ValueError as unpad_ex:
                _LOGGER.error("数据去填充失败: %s", unpad_ex)
                return None
                
            # 4. 解析 JSON
            try:
                data_str = unpadded_data_bytes.decode('utf-8')
                return json.loads(data_str)
            except Exception as json_ex:
                _LOGGER.error("数据解析失败: %s", json_ex)
                return None
            
        except Exception as e:
            _LOGGER.error("解密过程发生未知错误: %s", e)
            return None
