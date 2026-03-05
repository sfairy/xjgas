"""
新疆燃气集成配置流与选项流。
负责引导用户输入账号密码、校验登录并创建配置条目，同时提供计费模式等选项设置。
"""
import logging
import traceback
import voluptuous as vol
from homeassistant import config_entries
import homeassistant.helpers.config_validation as cv
try:
    from homeassistant.helpers import selector
except Exception:
    selector = None

from .const import (
    DOMAIN, CONF_PHONE, CONF_PASSWORD,
    CONF_UTILITY_TYPE,
    UTILITY_TYPE_GAS, DEFAULT_UTILITY_TYPE,
    CONF_PRICING_MODE, CONF_FIXED_PRICE,
    CONF_TIER_1_LIMIT, CONF_TIER_1_PRICE,
    CONF_TIER_2_LIMIT, CONF_TIER_2_PRICE,
    CONF_TIER_3_PRICE,
    PRICING_MODE_TIERED, PRICING_MODE_FIXED,
    DEFAULT_PRICING_MODE, DEFAULT_FIXED_PRICE,
    DEFAULT_TIER_1_LIMIT, DEFAULT_TIER_1_PRICE,
    DEFAULT_TIER_2_LIMIT, DEFAULT_TIER_2_PRICE,
    DEFAULT_TIER_3_PRICE,
    resolve_utility_type
)

_LOGGER = logging.getLogger(__name__)

class XjGasFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """
    处理配置流，创建唯一配置条目。
    """
    VERSION = 1

    async def async_step_user(self, user_input=None):
        """
        第一步：直接进入燃气登录。
        """
        return await self.async_step_gas(user_input)

    async def async_step_gas(self, user_input=None):
        """
        处理燃气登录步骤。
        
        Args:
            user_input (dict, optional): 用户提交的账号密码。
            
        Returns:
            FlowResult: 完成配置或显示错误。
        """
        errors = {}

        if user_input is not None:
            # 读取用户输入的账号与密码，用于登录校验
            phone = user_input.get(CONF_PHONE)
            password = user_input.get(CONF_PASSWORD)

            if not phone or not password:
                # 触发翻译文件中的错误提示
                errors["base"] = "missing_fields"
            else:
                try:
                    # 登录校验放在后台线程执行，避免阻塞主事件循环
                    from .api import XjGasAPI
                    api = XjGasAPI(phone=phone, password=password)
                    
                    success = await self.hass.async_add_executor_job(api.login)
                    
                    if success:
                        # 使用手机号作为唯一标识，避免重复添加相同账号
                        await self.async_set_unique_id(f"{UTILITY_TYPE_GAS}-{phone}")
                        self._abort_if_unique_id_configured()
                        
                        return self.async_create_entry(
                            title=f"新疆燃气 ({phone})",
                            data={
                                CONF_UTILITY_TYPE: UTILITY_TYPE_GAS,
                                CONF_PHONE: phone,
                                CONF_PASSWORD: password,
                            }
                        )
                    else:
                        errors["base"] = "invalid_auth"
                        
                except Exception as e:
                    _LOGGER.error("参数校验失败: %s", e)
                    errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="gas",
            data_schema=vol.Schema({
                vol.Required(CONF_PHONE): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
            }),
            errors=errors,
        )


    @staticmethod
    def async_get_options_flow(config_entry):
        """
        获取选项流处理器。
        """
        return XjGasOptionsFlowHandler(config_entry)

class XjGasOptionsFlowHandler(config_entries.OptionsFlow):
    """
    燃气选项流处理器。
    
    允许用户配置计费模式（阶梯计价/固定单价）和具体价格。
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """初始化选项流。"""
        self._config_entry = config_entry
        self._options = dict(config_entry.options)
        self._pricing_mode = None

    @property
    def config_entry(self):
        """覆盖 config_entry 属性以确保兼容性。"""
        return self._config_entry

    async def async_step_init(self, user_input=None):
        """
        初始化选项流步骤。
        
        显示菜单供用户选择要配置的项目。
        """
        return self.async_show_menu(
            step_id="init",
            menu_options=["pricing"]
        )

    async def async_step_pricing(self, user_input=None):
        """跳转到计费模式选择。"""
        return await self.async_step_mode(user_input)

    async def async_step_mode(self, user_input=None):
        """
        选择计费模式。
        
        用户可以选择阶梯计价或固定单价。
        """
        if user_input is not None:
            self._pricing_mode = user_input.get(CONF_PRICING_MODE, DEFAULT_PRICING_MODE)
            if self._pricing_mode == PRICING_MODE_FIXED:
                return await self.async_step_fixed()
            return await self.async_step_tiered()

        try:
            pricing_mode = self._options.get(CONF_PRICING_MODE, DEFAULT_PRICING_MODE)
            if pricing_mode not in (PRICING_MODE_TIERED, PRICING_MODE_FIXED):
                pricing_mode = DEFAULT_PRICING_MODE

            if selector:
                data_schema = vol.Schema({
                    vol.Required(CONF_PRICING_MODE, default=pricing_mode): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(value=PRICING_MODE_TIERED, label="阶梯计价"),
                                selector.SelectOptionDict(value=PRICING_MODE_FIXED, label="固定单价"),
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                })
            else:
                data_schema = vol.Schema({
                    vol.Required(CONF_PRICING_MODE, default=pricing_mode): vol.In([PRICING_MODE_TIERED, PRICING_MODE_FIXED]),
                })

            return self.async_show_form(
                step_id="mode",
                data_schema=data_schema
            )
        except Exception as e:
            _LOGGER.exception("选项流程中出现异常")
            return self.async_abort(reason="unknown_error")

    async def async_step_fixed(self, user_input=None):
        """
        配置固定单价。
        
        用户输入固定的燃气单价。
        """
        if user_input is not None:
            data = dict(self._options)
            data.update(user_input)
            data[CONF_PRICING_MODE] = PRICING_MODE_FIXED
            return self.async_create_entry(title="", data=data)

        fixed_price = self._options.get(CONF_FIXED_PRICE, DEFAULT_FIXED_PRICE)
        data_schema = vol.Schema({
            vol.Required(CONF_FIXED_PRICE, default=float(fixed_price)): vol.Coerce(float),
        })
        return self.async_show_form(
            step_id="fixed",
            data_schema=data_schema
        )

    async def async_step_tiered(self, user_input=None):
        """
        配置阶梯计价。
        
        用户输入各阶梯的用量限制和单价。
        """
        if user_input is not None:
            data = dict(self._options)
            data.update(user_input)
            data[CONF_PRICING_MODE] = PRICING_MODE_TIERED
            return self.async_create_entry(title="", data=data)

        tier_1_limit = self._options.get(CONF_TIER_1_LIMIT, DEFAULT_TIER_1_LIMIT)
        tier_1_price = self._options.get(CONF_TIER_1_PRICE, DEFAULT_TIER_1_PRICE)
        tier_2_limit = self._options.get(CONF_TIER_2_LIMIT, DEFAULT_TIER_2_LIMIT)
        tier_2_price = self._options.get(CONF_TIER_2_PRICE, DEFAULT_TIER_2_PRICE)
        tier_3_price = self._options.get(CONF_TIER_3_PRICE, DEFAULT_TIER_3_PRICE)

        data_schema = vol.Schema({
            vol.Optional(CONF_TIER_1_LIMIT, default=int(tier_1_limit)): vol.Coerce(int),
            vol.Optional(CONF_TIER_1_PRICE, default=float(tier_1_price)): vol.Coerce(float),
            vol.Optional(CONF_TIER_2_LIMIT, default=int(tier_2_limit)): vol.Coerce(int),
            vol.Optional(CONF_TIER_2_PRICE, default=float(tier_2_price)): vol.Coerce(float),
            vol.Optional(CONF_TIER_3_PRICE, default=float(tier_3_price)): vol.Coerce(float),
        })
        return self.async_show_form(
            step_id="tiered",
            data_schema=data_schema
        )

