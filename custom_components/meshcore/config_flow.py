"""Config flow for MeshCore integration."""
import logging
import asyncio
import os
from typing import Any, Dict, Optional

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv
from bleak import BleakScanner
from meshcore.events import EventType

from .const import (
    CONF_NAME,
    CONF_PUBKEY,
    DOMAIN,
    CONF_CONNECTION_TYPE,
    CONF_USB_PATH,
    CONF_BLE_ADDRESS,
    CONF_TCP_HOST,
    CONF_TCP_PORT,
    CONF_BAUDRATE,
    CONNECTION_TYPE_USB,
    CONNECTION_TYPE_BLE,
    CONNECTION_TYPE_TCP,
    DEFAULT_BAUDRATE,
    DEFAULT_TCP_PORT,
    CONNECTION_TIMEOUT,
    CONF_REPEATER_SUBSCRIPTIONS,
    CONF_REPEATER_NAME,
    CONF_REPEATER_PASSWORD,
    CONF_REPEATER_UPDATE_INTERVAL,
    DEFAULT_REPEATER_UPDATE_INTERVAL,
    CONF_INFO_INTERVAL,
    CONF_MESSAGES_INTERVAL,
    DEFAULT_INFO_INTERVAL,
    DEFAULT_MESSAGES_INTERVAL,
    NodeType,
)
from .meshcore_api import MeshCoreAPI

_LOGGER = logging.getLogger(__name__)

class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONNECTION_TYPE): vol.In(
            [CONNECTION_TYPE_USB, CONNECTION_TYPE_BLE, CONNECTION_TYPE_TCP]
        ),
    }
)

USB_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USB_PATH): str,
        vol.Optional(CONF_BAUDRATE, default=DEFAULT_BAUDRATE): cv.positive_int,
        vol.Optional(
            CONF_MESSAGES_INTERVAL, 
            default=DEFAULT_MESSAGES_INTERVAL,
            description="How often to check for new messages (seconds)"
        ): vol.All(cv.positive_int, vol.Range(min=5, max=60)),
        vol.Optional(
            CONF_INFO_INTERVAL, 
            default=DEFAULT_INFO_INTERVAL,
            description="How often to update device info and contacts (seconds)"
        ): vol.All(cv.positive_int, vol.Range(min=30, max=300)),
    }
)

BLE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BLE_ADDRESS): str,
        vol.Optional(
            CONF_MESSAGES_INTERVAL, 
            default=DEFAULT_MESSAGES_INTERVAL,
            description="How often to check for new messages (seconds)"
        ): vol.All(cv.positive_int, vol.Range(min=5, max=60)),
        vol.Optional(
            CONF_INFO_INTERVAL, 
            default=DEFAULT_INFO_INTERVAL,
            description="How often to update device info and contacts (seconds)"
        ): vol.All(cv.positive_int, vol.Range(min=30, max=300)),
    }
)

TCP_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TCP_HOST): str,
        vol.Optional(CONF_TCP_PORT, default=DEFAULT_TCP_PORT): cv.port,
        vol.Optional(
            CONF_MESSAGES_INTERVAL, 
            default=DEFAULT_MESSAGES_INTERVAL,
            description="How often to check for new messages (seconds)"
        ): vol.All(cv.positive_int, vol.Range(min=5, max=60)),
        vol.Optional(
            CONF_INFO_INTERVAL, 
            default=DEFAULT_INFO_INTERVAL,
            description="How often to update device info and contacts (seconds)"
        ): vol.All(cv.positive_int, vol.Range(min=30, max=300)),
    }
)

async def validate_common(api: MeshCoreAPI) -> Dict[str, Any]:
    """Validate the user input allows us to connect to the USB device."""
    try: 
        # Try to connect with timeout
        connect_success = await asyncio.wait_for(api.connect(), timeout=CONNECTION_TIMEOUT)
        
        # Check if connection was successful
        if not connect_success or not api._mesh_core:
            _LOGGER.error("Failed to connect to device - connect() returned False")
            raise CannotConnect("Device connection failed")
            
        # Get node info to verify communication
        node_info = await api._mesh_core.commands.send_appstart()
        
        # Validate we got meaningful info back
        if node_info.type == EventType.ERROR:
            _LOGGER.error("Failed to get node info - received error: %s", node_info.payload)
            raise CannotConnect("Failed to get node info")
            
        # Disconnect when done
        await api.disconnect()
        
        # Extract and log the device information
        device_name = node_info.payload.get('name', 'Unknown')
        public_key = node_info.payload.get('public_key', '')
        
        # Log the values we're extracting
        _LOGGER.info(f"Validating device - Name: {device_name}, Public Key: {public_key[:10]}")
        
        # If we get here, the connection was successful and we got valid info
        return {"title": f"MeshCore Node {device_name}", "name": device_name, "pubkey": public_key}
    except asyncio.TimeoutError:
        raise CannotConnect("Connection timed out")
    except Exception as ex:
        _LOGGER.error("Validation error: %s", ex)
        raise CannotConnect(f"Failed to connect: {str(ex)}")

async def validate_usb_input(hass: HomeAssistant, data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the user input allows us to connect to the USB device."""
    api = MeshCoreAPI(
        hass=hass,
        connection_type=CONNECTION_TYPE_USB,
        usb_path=data[CONF_USB_PATH],
        baudrate=data[CONF_BAUDRATE],
    )
    return await validate_common(api)


async def validate_ble_input(hass: HomeAssistant, data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the user input allows us to connect to the BLE device."""
    api = MeshCoreAPI(
        hass=hass,
        connection_type=CONNECTION_TYPE_BLE,
        ble_address=data[CONF_BLE_ADDRESS],
    ) 
    return await validate_common(api)


async def validate_tcp_input(hass: HomeAssistant, data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the user input allows us to connect to the TCP device."""
    api = MeshCoreAPI(
        hass=hass,
        connection_type=CONNECTION_TYPE_TCP,
        tcp_host=data[CONF_TCP_HOST],
        tcp_port=data[CONF_TCP_PORT],
    )
    return await validate_common(api)


class MeshCoreConfigFlow(config_entries.ConfigFlow, domain=DOMAIN): # type: ignore
    """Handle a config flow for MeshCore."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize flow."""
        self.connection_type: Optional[str] = None
        self.discovery_info: Optional[Dict[str, Any]] = None
        
    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return OptionsFlowHandler(config_entry)

    async def async_step_user(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        """Handle the initial step."""
        errors: Dict[str, str] = {}

        if user_input is not None:
            self.connection_type = user_input[CONF_CONNECTION_TYPE]
            
            if self.connection_type == CONNECTION_TYPE_USB:
                return await self.async_step_usb()
            if self.connection_type == CONNECTION_TYPE_BLE:
                return await self.async_step_ble()
            if self.connection_type == CONNECTION_TYPE_TCP:
                return await self.async_step_tcp()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_usb(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        """Handle USB configuration."""
        errors: Dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_usb_input(self.hass, user_input)
                return self.async_create_entry(title=info["title"], data={
                    CONF_CONNECTION_TYPE: CONNECTION_TYPE_USB,
                    CONF_USB_PATH: user_input[CONF_USB_PATH],
                    CONF_BAUDRATE: user_input[CONF_BAUDRATE],
                    CONF_NAME: info.get("name"),
                    CONF_PUBKEY: info.get("pubkey"),
                    CONF_MESSAGES_INTERVAL: user_input.get(CONF_MESSAGES_INTERVAL, DEFAULT_MESSAGES_INTERVAL),
                    CONF_INFO_INTERVAL: user_input.get(CONF_INFO_INTERVAL, DEFAULT_INFO_INTERVAL),
                    CONF_REPEATER_SUBSCRIPTIONS: [],  # Initialize with empty repeater subscriptions
                })
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        # Always allow manual entry for USB path
        # Skip trying to detect ports completely
        return self.async_show_form(
            step_id="usb", 
            data_schema=vol.Schema({
                vol.Required(CONF_USB_PATH): str,
                vol.Optional(CONF_BAUDRATE, default=DEFAULT_BAUDRATE): cv.positive_int,
                vol.Optional(
                    CONF_MESSAGES_INTERVAL,
                    default=DEFAULT_MESSAGES_INTERVAL
                ): int,
                vol.Optional(
                    CONF_INFO_INTERVAL,
                    default=DEFAULT_INFO_INTERVAL
                ): int,
            }),
            errors=errors
        )

    async def async_step_ble(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        """Handle BLE configuration."""
        errors: Dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_ble_input(self.hass, user_input)
                return self.async_create_entry(title=info["title"], data={
                    CONF_CONNECTION_TYPE: CONNECTION_TYPE_BLE,
                    CONF_BLE_ADDRESS: user_input[CONF_BLE_ADDRESS],
                    CONF_NAME: info.get("name"),
                    CONF_PUBKEY: info.get("pubkey"),
                    CONF_MESSAGES_INTERVAL: user_input.get(CONF_MESSAGES_INTERVAL, DEFAULT_MESSAGES_INTERVAL),
                    CONF_INFO_INTERVAL: user_input.get(CONF_INFO_INTERVAL, DEFAULT_INFO_INTERVAL),
                    CONF_REPEATER_SUBSCRIPTIONS: [],  # Initialize with empty repeater subscriptions
                })
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        # Scan for BLE devices
        devices = {}
        try:
            scanner = BleakScanner()
            discovered_devices = await scanner.discover(timeout=5.0)
            for device in discovered_devices:
                if device.name and "MeshCore" in device.name:
                    devices[device.address] = f"{device.name} ({device.address})"
        except Exception as ex:
            _LOGGER.warning("Failed to scan for BLE devices: %s", ex)

        # If we have discovered devices, show them in a dropdown
        if devices:
            schema = vol.Schema(
                {
                    vol.Required(CONF_BLE_ADDRESS): vol.In(devices),
                    vol.Optional(
                        CONF_MESSAGES_INTERVAL,
                        default=DEFAULT_MESSAGES_INTERVAL
                    ): int,
                    vol.Optional(
                        CONF_INFO_INTERVAL,
                        default=DEFAULT_INFO_INTERVAL
                    ): int,
                }
            )
        else:
            # Otherwise, allow manual entry, but with simplified schema
            schema = vol.Schema({
                vol.Required(CONF_BLE_ADDRESS): str,
                vol.Optional(
                    CONF_MESSAGES_INTERVAL,
                    default=DEFAULT_MESSAGES_INTERVAL
                ): int,
                vol.Optional(
                    CONF_INFO_INTERVAL,
                    default=DEFAULT_INFO_INTERVAL
                ): int,
            })

        return self.async_show_form(
            step_id="ble", data_schema=schema, errors=errors
        )

    async def async_step_tcp(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        """Handle TCP configuration."""
        errors: Dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_tcp_input(self.hass, user_input)
                return self.async_create_entry(title=info["title"], data={
                    CONF_CONNECTION_TYPE: CONNECTION_TYPE_TCP,
                    CONF_TCP_HOST: user_input[CONF_TCP_HOST],
                    CONF_TCP_PORT: user_input[CONF_TCP_PORT],
                    CONF_NAME: info.get("name"),
                    CONF_PUBKEY: info.get("pubkey"),
                    CONF_MESSAGES_INTERVAL: user_input.get(CONF_MESSAGES_INTERVAL, DEFAULT_MESSAGES_INTERVAL),
                    CONF_INFO_INTERVAL: user_input.get(CONF_INFO_INTERVAL, DEFAULT_INFO_INTERVAL),
                    CONF_REPEATER_SUBSCRIPTIONS: [],  # Initialize with empty repeater subscriptions
                })
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="tcp", 
            data_schema=vol.Schema({
                vol.Required(CONF_TCP_HOST): str,
                vol.Optional(CONF_TCP_PORT, default=DEFAULT_TCP_PORT): cv.port,
                vol.Optional(
                    CONF_MESSAGES_INTERVAL,
                    default=DEFAULT_MESSAGES_INTERVAL
                ): int,
                vol.Optional(
                    CONF_INFO_INTERVAL,
                    default=DEFAULT_INFO_INTERVAL
                ): int,
            }),
            errors=errors
        )


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for MeshCore."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self.config_entry = config_entry
        self.repeater_subscriptions = list(config_entry.data.get(CONF_REPEATER_SUBSCRIPTIONS, []))
        self.hass = None

    async def async_step_init(self, user_input=None):
        """Handle options flow."""
        if user_input is not None:
            # Get the action from the input
            action = user_input.get("action")
            
            if action == "add_repeater":
                # Go to add repeater screen
                return await self.async_step_add_repeater()
                
            elif action == "remove_repeater" and user_input.get("repeater_to_remove"):
                # Remove the selected repeater
                repeater_to_remove = user_input.get("repeater_to_remove")
                
                # Get current repeater list
                current_repeaters = self.repeater_subscriptions.copy()
                
                # Update the list without the removed repeater
                self.repeater_subscriptions = [
                    r for r in self.repeater_subscriptions 
                    if r.get("name") != repeater_to_remove
                ]
                
                # Update the config entry data
                new_data = dict(self.config_entry.data)
                new_data[CONF_REPEATER_SUBSCRIPTIONS] = self.repeater_subscriptions
                self.hass.config_entries.async_update_entry(self.config_entry, data=new_data) # type: ignore
                
                # Return to the init step to show updated list
                return await self.async_step_init()
                
            else:
                # Save options
                new_options = {
                    CONF_INFO_INTERVAL: user_input.get(CONF_INFO_INTERVAL, DEFAULT_INFO_INTERVAL),
                    CONF_MESSAGES_INTERVAL: user_input.get(CONF_MESSAGES_INTERVAL, DEFAULT_MESSAGES_INTERVAL),
                }
                return self.async_create_entry(title="", data=new_options)

        # Show the form with action options and repeater list
        options = self.config_entry.options
        
        # Build the schema with a list of options
        schema = {
            vol.Optional(
                CONF_MESSAGES_INTERVAL,
                default=options.get(CONF_MESSAGES_INTERVAL, DEFAULT_MESSAGES_INTERVAL)
            ): int,
            
            vol.Optional(
                CONF_INFO_INTERVAL,
                default=options.get(CONF_INFO_INTERVAL, DEFAULT_INFO_INTERVAL)
            ): int,
            
            vol.Optional(
                "action"
            ): vol.In({
                "add_repeater": "Add Repeater",
                "remove_repeater": "Remove Repeater",
            }),
        }
        
        # If there are repeaters and the action is remove, add a selection dropdown
        if self.repeater_subscriptions and "action" in schema:
            repeater_names = {r.get("name"): r.get("name") for r in self.repeater_subscriptions}
            if repeater_names:
                schema["repeater_to_remove"] = vol.In(repeater_names)
        
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
            description_placeholders={
                "repeaters": ", ".join([r.get("name", "Unknown") for r in self.repeater_subscriptions]) or "None configured"
            },
        )
        
        
    def _get_repeater_contacts(self):
        """Get repeater contacts from coordinator's cached data."""
        # Get the coordinator
        coordinator = self.hass.data[DOMAIN].get(self.config_entry.entry_id) # type: ignore
        if not coordinator or not coordinator.data:
            return []
            
        # Extract repeater contacts from cached data
        contacts = coordinator.data.get("contacts", [])
        repeater_contacts = []
        
        for contact in contacts:
            if not isinstance(contact, dict):
                continue
                
            contact_name = contact.get("adv_name", "")
            contact_type = contact.get("type")
            
            # Type 2 indicates a repeater
            if contact_name and contact_type == NodeType.REPEATER or contact_type == NodeType.ROOM_SERVER:
                repeater_contacts.append(contact_name)
                
        return repeater_contacts
        
    async def async_step_add_repeater(self, user_input=None):
        """Handle adding a new repeater subscription."""
        errors = {}
        
        if user_input is not None:
            repeater_name = user_input.get(CONF_REPEATER_NAME)
            password = user_input.get(CONF_REPEATER_PASSWORD)
            update_interval = user_input.get(CONF_REPEATER_UPDATE_INTERVAL, DEFAULT_REPEATER_UPDATE_INTERVAL)
            
            # Check if this repeater is already in the subscriptions
            existing_names = [r.get("name") for r in self.repeater_subscriptions]
            if repeater_name in existing_names:
                errors["repeater_name"] = "already_configured"
            else:
                # Add the new repeater subscription
                self.repeater_subscriptions.append({
                    "name": repeater_name,
                    "password": password,
                    "update_interval": update_interval,
                    "enabled": True,
                })
                
                # Update the config entry data
                new_data = dict(self.config_entry.data)
                new_data[CONF_REPEATER_SUBSCRIPTIONS] = self.repeater_subscriptions
                self.hass.config_entries.async_update_entry(self.config_entry, data=new_data) # type: ignore
                
                # Return to the init step
                return await self.async_step_init() # type: ignore
        
        # Get repeater contacts
        repeater_contacts = self._get_repeater_contacts()
        
        # Show the form with repeater selection
        if not repeater_contacts:
            # No repeaters found
            return self.async_show_form(
                step_id="add_repeater",
                data_schema=vol.Schema({
                    vol.Required("no_repeaters", default="No repeaters found in contacts. Please ensure your device has repeaters in its contacts list."): str,
                }),
                errors=errors,
            )
        
        # Contacts found, show selection form
        return self.async_show_form(
            step_id="add_repeater",
            data_schema=vol.Schema({
                vol.Required(CONF_REPEATER_NAME): vol.In({name: name for name in repeater_contacts}),
                vol.Optional(CONF_REPEATER_PASSWORD, default=""): str,
                vol.Optional(CONF_REPEATER_UPDATE_INTERVAL, default=DEFAULT_REPEATER_UPDATE_INTERVAL): int,
            }),
            errors=errors,
        )
        
