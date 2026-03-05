"""
新疆燃气集成的主入口文件。
此文件负责集成的初始化、配置条目设置以及卸载流程。
"""
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, CoreState, EVENT_HOMEASSISTANT_STARTED
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.frontend import add_extra_js_url

# 引入常量定义，确保域名一致
from .const import (
    DOMAIN,
    VERSION,
    CONF_PRICING_MODE,
    DEFAULT_PRICING_MODE,
    PRICING_MODE_FIXED,
)

_LOGGER = logging.getLogger(__name__)

# 定义集成支持的平台列表
# 目前仅支持 sensor (传感器) 平台，后续扩展在此追加
PLATFORMS: list[str] = ["sensor"]

# 卡片静态路径与文件名
CARD_URL_BASE = "/xjgas-local"
CARD_FILENAME = "xjgas-card.js"


async def async_setup(hass: HomeAssistant, config: dict):
    """
    通过 configuration.yaml 设置集成。
    
    这是旧版的设置方式。对于现代集成，推荐使用 Config Flow (UI 配置)。
    如果用户尝试通过 YAML 配置，此函数将被调用。
    
    Args:
        hass (HomeAssistant): Home Assistant 核心实例。
        config (dict): 全局配置字典。
        
    Returns:
        bool: 设置是否成功。此处始终返回 True，表示允许通过 UI 继续配置。
    """
    async def _register_frontend(_event=None) -> None:
        await _setup_xjgas_card(hass)

    # 必须在 async_setup 中注册，且需在 HA 启动完成后执行
    if hass.state == CoreState.running:
        await _register_frontend()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _register_frontend)
    return True


async def _setup_xjgas_card(hass: HomeAssistant) -> bool:
    """
    设置新疆燃气卡片前端资源。
    
    1. 注册静态路径，使 /xjgas-local 可访问 www 目录
    2. 将卡片添加到 Lovelace 资源（storage 模式下），使卡片出现在仪表板资源列表
    """
    www_dir = hass.config.path("custom_components/xjgas/www")
    try:
        await hass.http.async_register_static_paths([
            StaticPathConfig(CARD_URL_BASE, www_dir, False)
        ])
        _LOGGER.debug("register_static_path: %s -> %s", CARD_URL_BASE, www_dir)
    except RuntimeError:
        _LOGGER.debug("Static path %s already registered", CARD_URL_BASE)

    add_extra_js_url(hass, f"{CARD_URL_BASE}/{CARD_FILENAME}?v={VERSION}")

    # 将卡片注册到 Lovelace 资源，使仪表板「添加卡片」时能看到
    lovelace = hass.data.get("lovelace")
    if lovelace is None:
        _LOGGER.debug("Lovelace not loaded, skipping resource registration")
        return True

    mode = getattr(lovelace, "mode", getattr(lovelace, "resource_mode", "yaml"))
    if mode != "storage":
        _LOGGER.debug("Lovelace mode is %s, skipping resource registration (storage required)", mode)
        return True

    async def _register_resource(_now: Any) -> None:
        if not getattr(lovelace.resources, "loaded", False):
            _LOGGER.debug("Lovelace resources not loaded, retrying in 5s")
            async_call_later(hass, 5, _register_resource)
            return

        url = f"{CARD_URL_BASE}/{CARD_FILENAME}?v={VERSION}"
        existing = [r for r in lovelace.resources.async_items() if r.get("url", "").startswith(f"{CARD_URL_BASE}/{CARD_FILENAME}")]
        if existing:
            for r in existing:
                if r.get("url") != url:
                    try:
                        await lovelace.resources.async_update_item(r["id"], {"res_type": "module", "url": url})
                        _LOGGER.info("Updated xjgas card resource to v%s", VERSION)
                    except Exception as e:
                        _LOGGER.warning("Failed to update card resource: %s", e)
            return

        try:
            await lovelace.resources.async_create_item({"res_type": "module", "url": url})
            _LOGGER.info("Registered xjgas card in Lovelace resources")
        except Exception as e:
            _LOGGER.warning("Failed to register card resource: %s", e)

    await _register_resource(0)
    return True


async def setup_xjgas_card(hass: HomeAssistant) -> bool:
    """兼容旧调用：在 async_setup_entry 中仅注册静态路径，资源由 async_setup 统一处理。"""
    www_dir = hass.config.path("custom_components/xjgas/www")
    try:
        await hass.http.async_register_static_paths([
            StaticPathConfig(CARD_URL_BASE, www_dir, False)
        ])
    except RuntimeError:
        pass
    add_extra_js_url(hass, f"{CARD_URL_BASE}/{CARD_FILENAME}?v={VERSION}")
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """
    通过 Config Entry (UI 配置条目) 设置集成。
    
    当用户在 UI 中添加集成，或 Home Assistant 启动并加载已保存的配置时，此函数被调用。
    
    Args:
        hass (HomeAssistant): Home Assistant 核心实例。
        entry (ConfigEntry): 当前的配置条目实例，包含用户输入的配置数据。
        
    Returns:
        bool: 设置是否成功。
    """
    # 添加前端资源
    await setup_xjgas_card(hass)

    # 转发 setup 到各个平台
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # 监听配置更新
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True

async def async_update_options(hass: HomeAssistant, entry: ConfigEntry):
    """
    当选项更新时重新加载集成。
    
    Args:
        hass (HomeAssistant): Home Assistant 核心实例。
        entry (ConfigEntry): 配置条目。
    """
    # 燃气类型的特殊处理：更新实体注册表中的禁用状态
    registry = er.async_get(hass)
    pricing_mode = entry.options.get(CONF_PRICING_MODE, DEFAULT_PRICING_MODE)
    # 如果是固定价格模式，禁用阶梯相关的实体
    disabled_by = er.RegistryEntryDisabler.INTEGRATION if pricing_mode == PRICING_MODE_FIXED else None
    
    for entity in registry.entities.values():
        if entity.config_entry_id != entry.entry_id:
            continue
        if entity.domain != "sensor":
            continue
        unique_id = entity.unique_id or ""
        # 查找当前阶梯实体
        if not unique_id.endswith("_current_tier"):
            continue
        if entity.disabled_by == disabled_by:
            continue
        # 更新实体的禁用状态
        registry.async_update_entity(entity.entity_id, disabled_by=disabled_by)
        
    # 重载配置条目以应用更改
    await hass.config_entries.async_reload(entry.entry_id)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """
    卸载配置条目。
    
    当用户移除集成、禁用集成或修改配置导致重新加载时调用。
    负责清理资源、取消监听器等。
    
    Args:
        hass (HomeAssistant): Home Assistant 核心实例。
        entry (ConfigEntry): 要卸载的配置条目。
        
    Returns:
        bool: 卸载是否成功。如果所有平台都成功卸载，则返回 True。
    """
    # 卸载所有加载的平台，释放实体与协调器
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    return unload_ok
