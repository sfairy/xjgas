"""
XjGas 集成的常量定义文件。
此文件包含集成中使用的全局常量，如配置键名、默认值等。
"""

# 定义集成的域名，用于在 Home Assistant 中唯一标识此集成
# 这必须与 manifest.json 中的 domain 字段匹配
DOMAIN = "xjgas"

# 集成版本号，用于控制前端资源缓存
# 每次修改前端代码后，请更新此版本号
VERSION = "1.0.0"

# 配置键名常量
# 用于在配置流 (config_flow) 和选项流 (options_flow) 中引用用户输入的字段
CONF_PHONE = "phone"       # 手机号字段键名
CONF_PASSWORD = "password" # 密码字段键名
# 集成类型与账号字段
CONF_UTILITY_TYPE = "utility_type"
UTILITY_TYPE_GAS = "gas"
DEFAULT_UTILITY_TYPE = UTILITY_TYPE_GAS

# 计费配置常量
CONF_PRICING_MODE = "pricing_mode"
CONF_FIXED_PRICE = "fixed_price"
CONF_TIER_1_LIMIT = "tier_1_limit"
CONF_TIER_1_PRICE = "tier_1_price"
CONF_TIER_2_LIMIT = "tier_2_limit"
CONF_TIER_2_PRICE = "tier_2_price"
CONF_TIER_3_PRICE = "tier_3_price"

# 计费模式选项
PRICING_MODE_TIERED = "tiered"
PRICING_MODE_FIXED = "fixed"

# 默认计费配置
DEFAULT_PRICING_MODE = PRICING_MODE_TIERED
DEFAULT_FIXED_PRICE = 1.50
DEFAULT_TIER_1_LIMIT = 300
DEFAULT_TIER_1_PRICE = 1.50
DEFAULT_TIER_2_LIMIT = 400
DEFAULT_TIER_2_PRICE = 1.80
DEFAULT_TIER_3_PRICE = 2.25


def resolve_utility_type(entry_data: dict) -> str:
    """
    根据配置数据解析公用事业类型（仅支持燃气）。
    
    Args:
        entry_data (dict): 配置数据。
        
    Returns:
        str: 'gas'。
    """
    return UTILITY_TYPE_GAS
