"""Services for the MeshCore integration."""
import logging
import time
import voluptuous as vol
import json
from typing import Any, Dict, Optional

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.const import MAJOR_VERSION
from meshcore.events import EventType

from .const import (
    ATTR_PUBKEY_PREFIX,
    DOMAIN, 
    SERVICE_SEND_MESSAGE,
    SERVICE_SEND_CHANNEL_MESSAGE,
    SERVICE_CLI_COMMAND,
    SERVICE_MESSAGE_SCRIPT,
    SERVICE_EXECUTE_CLI_COMMAND_UI,
    ATTR_NODE_ID,
    ATTR_CHANNEL_IDX,
    ATTR_MESSAGE,
    ATTR_CLI_COMMAND,
    ATTR_ENTRY_ID,
    DEFAULT_DEVICE_NAME,
)

_LOGGER = logging.getLogger(__name__)

# Schema for send_message service with either node_id or pubkey_prefix required
SEND_MESSAGE_SCHEMA = vol.Schema(
    {
        vol.Exclusive(ATTR_NODE_ID, 'target'): cv.string,
        vol.Exclusive(ATTR_PUBKEY_PREFIX, 'target'): cv.string,
        vol.Required(ATTR_MESSAGE): cv.string,
        vol.Optional(ATTR_ENTRY_ID): cv.string,
    }
).extend({vol.Required: vol.Any(ATTR_NODE_ID, ATTR_PUBKEY_PREFIX)})

# Schema for send_channel_message service
SEND_CHANNEL_MESSAGE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CHANNEL_IDX): cv.positive_int,
        vol.Required(ATTR_MESSAGE): cv.string,
        vol.Optional(ATTR_ENTRY_ID): cv.string,
    }
)

# Schema for cli_command service
CLI_COMMAND_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CLI_COMMAND): cv.string,
        vol.Optional(ATTR_ENTRY_ID): cv.string,
    }
)

# Schema for UI message service (no parameters needed)
UI_MESSAGE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): cv.string,
    }
)

async def async_setup_services(hass: HomeAssistant) -> None:
    """Set up services for MeshCore integration."""
    
    async def async_send_message_service(call: ServiceCall) -> None:
        """Handle sending a message service call."""
        message = call.data[ATTR_MESSAGE]
        entry_id = call.data.get(ATTR_ENTRY_ID)
        
        # Check which target identifier was provided
        if ATTR_NODE_ID in call.data:
            # Sending by node_id (friendly name)
            node_id = call.data[ATTR_NODE_ID]
            pubkey_prefix = None
            target_identifier = f"node_id '{node_id}'"
        else:
            # Sending by public key
            node_id = None
            pubkey_prefix = call.data[ATTR_PUBKEY_PREFIX]
            target_identifier = f"public key '{pubkey_prefix}'"
        
        # Iterate through all registered config entries
        for config_entry_id, coordinator in hass.data[DOMAIN].items():
            _LOGGER.debug("Entry ID: %s, coordinator: %s", config_entry_id, coordinator)
            # If entry_id is specified, only use the matching entry
            if entry_id and entry_id != config_entry_id:
                continue
                
            # Get the API from coordinator
            api = coordinator.api
            if api and api.connected:
                try:
                    _LOGGER.debug(
                        "Sending message to %s: %s", target_identifier, message
                    )
                    
                    # Get the contact object
                    contact = None
                    contact_name = None
                    if node_id is not None:
                        # Find contact by name
                        contact = api.mesh_core.get_contact_by_name(node_id)
                        if not contact:
                            _LOGGER.error(f"Contact with name '{node_id}' not found")
                            continue
                    else:
                        # Find contact by pubkey prefix
                        contact = api.mesh_core.get_contact_by_key_prefix(pubkey_prefix)
                        if not contact:
                            _LOGGER.error(f"Contact with pubkey prefix '{pubkey_prefix}' not found")
                            continue
                    
                    # Send the message using the new API
                    result = await api.mesh_core.commands.send_msg(contact, message)
                    
                    if result.type == EventType.ERROR:
                        _LOGGER.warning(
                            "Failed to send message to %s: %s", target_identifier, result.payload
                        )
                    else:
                        # Use the actual contact name for logging when available
                        display_name = contact_name if contact_name else target_identifier
                        pubkey = contact.get("public_key", "Unknown")
                        _LOGGER.info("Successfully sent message to %s, pubkey: %s", display_name, pubkey)
                        
                        # Create outgoing message event data
                        outgoing_msg = {
                            "message": message,
                            "device": config_entry_id,
                            "message_type": "direct",
                            "receiver": contact.get("name"),
                            "timestamp": int(time.time()),
                            "contact_public_key": pubkey
                        }
                        # Fire event for outgoing message to update message-related entities
                        hass.bus.async_fire(f"{DOMAIN}_message_sent", outgoing_msg)
                except Exception as ex:
                    _LOGGER.error(
                        "Error sending message to %s: %s", target_identifier, ex
                    )
                # Only attempt with the first available API if no entry_id specified
                if not entry_id:
                    return
    
    async def async_send_channel_message_service(call: ServiceCall) -> None:
        """Handle sending a channel message service call."""
        channel_idx = call.data[ATTR_CHANNEL_IDX]
        message = call.data[ATTR_MESSAGE]
        entry_id = call.data.get(ATTR_ENTRY_ID)
        
        # Iterate through all registered config entries
        for config_entry_id, coordinator in hass.data[DOMAIN].items():
            _LOGGER.debug("Entry ID: %s, coordinator: %s", config_entry_id, coordinator.name)
            # If entry_id is specified, only use the matching entry
            if entry_id and entry_id != config_entry_id:
                continue
                
            # Get the API from coordinator
            api = coordinator.api
            if api and api.connected:
                try:
                    _LOGGER.debug(
                        "Sending message to channel %s: %s", channel_idx, message
                    )
                    
                    # Send the channel message using the new API
                    result = await api.mesh_core.commands.send_chan_msg(channel_idx, message)
                    
                    if result.type == EventType.ERROR:
                        _LOGGER.warning(
                            "Failed to send message to channel %s: %s", channel_idx, result.payload
                        )
                    else:
                        _LOGGER.info(
                            "Successfully sent message to channel %s", channel_idx
                        )
                        
                        # Create outgoing message event data
                        outgoing_msg = {
                            "message": message,
                            "device": config_entry_id,
                            "message_type": "channel",
                            "receiver": f"channel_{channel_idx}",
                            "timestamp": int(time.time()),
                            "channel_idx": channel_idx
                        }
                        # Fire event for outgoing message to update message-related entities
                        hass.bus.async_fire(f"{DOMAIN}_message_sent", outgoing_msg)
                except Exception as ex:
                    _LOGGER.error(
                        "Error sending message to channel %s: %s", channel_idx, ex
                    )
                # Only attempt with the first available API if no entry_id specified
                if not entry_id:
                    return
    
    # async def async_cli_command_service(call: ServiceCall) -> None:
    #     """Handle CLI command service call."""
    #     command = call.data[ATTR_CLI_COMMAND]
    #     entry_id = call.data.get(ATTR_ENTRY_ID)
        
    #     # Track if any command executed successfully
    #     command_success = False
        
    #     # Iterate through all registered config entries
    #     for config_entry_id, coordinator in hass.data[DOMAIN].items():            
    #         # If entry_id is specified, only use the matching entry
    #         if entry_id and entry_id != config_entry_id:
    #             continue
                
    #         # Get the API from coordinator
    #         api = coordinator.api
    #         if api and api._connected:
    #             try:
    #                 _LOGGER.debug(
    #                     "Executing CLI command: %s", command
    #                 )
                    
    #                 result = await api.send_cli_command(command)
                    
    #                 if result.get("success", False):
    #                     command_success = True
    #                     _LOGGER.info(
    #                         "Successfully executed CLI command: %s", command
    #                     )
    #                 else:
    #                     _LOGGER.warning(
    #                         "CLI command execution failed: %s, error: %s", 
    #                         command, result.get("error", "Unknown error")
    #                     )
                        
    #             except Exception as ex:
    #                 _LOGGER.error(
    #                     "Error executing CLI command %s: %s", command, ex
    #                 )
                
    #             # Only attempt with the first available API if no entry_id specified
    #             if not entry_id:
    #                 return
        
    #     if not command_success:
    #         _LOGGER.error("Failed to execute CLI command on any device: %s", command)

    # Create combined message script service
    async def async_message_script_service(call: ServiceCall) -> None:
        """Handle the combined messaging script service that works with UI helpers."""
        entry_id = call.data.get(ATTR_ENTRY_ID)
        
        # Get state from helper entities
        recipient_type = hass.states.get("select.meshcore_recipient_type")
        
        if not recipient_type:
            _LOGGER.error("Recipient type helper not found: select.meshcore_recipient_type")
            return
            
        # Get recipient type value
        recipient_type_value = recipient_type.state
        
        # Get message from text entity
        message_entity = hass.states.get("text.meshcore_message")
        if not message_entity:
            _LOGGER.error("Message input helper not found: text.meshcore_message")
            return
            
        message = message_entity.state
        
        if not message:
            _LOGGER.warning("No message to send - message input is empty")
            return
            
        # Handle based on recipient type
        if recipient_type_value == "Channel":
            # Get channel selection
            channel_entity = hass.states.get("select.meshcore_channel")
            if not channel_entity:
                _LOGGER.error("Channel helper not found: select.meshcore_channel")
                return
                
            channel_value = channel_entity.state
            
            # Parse channel number
            try:
                channel_idx = int(channel_value.replace("Channel ", ""))
            except (ValueError, AttributeError):
                _LOGGER.error(f"Invalid channel format: {channel_value}")
                return
                
            # Create channel message service call
            channel_call = create_service_call(
                DOMAIN, 
                SERVICE_SEND_CHANNEL_MESSAGE, 
                {"channel_idx": channel_idx, "message": message, "entry_id": entry_id}
            )
            
            # Send the channel message
            await async_send_channel_message_service(channel_call)
            
        elif recipient_type_value == "Contact":
            # Get contact selection
            contact_entity = hass.states.get("select.meshcore_contact")
            if not contact_entity:
                _LOGGER.error("Contact helper not found: select.meshcore_contact")
                return
                
            # Get the public key from attributes
            pubkey_prefix = contact_entity.attributes.get("public_key_prefix")
            if not pubkey_prefix:
                _LOGGER.error("Public key not found in contact attributes")
                return
                
            # Create contact message service call
            contact_call = create_service_call(
                DOMAIN, 
                SERVICE_SEND_MESSAGE, 
                {"pubkey_prefix": pubkey_prefix, "message": message, "entry_id": entry_id}
            )
            
            # Send the direct message
            await async_send_message_service(contact_call)
        else:
            _LOGGER.error(f"Unknown recipient type: {recipient_type_value}")
            
        # Clear the message input after sending
        try:
            await hass.services.async_call(
                "text", 
                "set_value", 
                {"entity_id": "text.meshcore_message", "value": ""},
                blocking=False
            )
        except Exception as ex:
            _LOGGER.warning(f"Could not clear message input: {ex}")
    
    # Register services
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        async_send_message_service,
        schema=SEND_MESSAGE_SCHEMA,
    )
    
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_CHANNEL_MESSAGE,
        async_send_channel_message_service,
        schema=SEND_CHANNEL_MESSAGE_SCHEMA,
    )
    
    # hass.services.async_register(
    #     DOMAIN,
    #     SERVICE_CLI_COMMAND,
    #     async_cli_command_service,
    #     schema=CLI_COMMAND_SCHEMA,
    # )
    
    # Register the combined UI message service
    hass.services.async_register(
        DOMAIN,
        SERVICE_MESSAGE_SCRIPT,
        async_message_script_service,
        schema=UI_MESSAGE_SCHEMA,
    )
    
    # Create CLI command execution service from UI helper
    # async def async_execute_cli_command_ui(call: ServiceCall) -> None:
    #     """Execute CLI command from the text helper entity."""
    #     entry_id = call.data.get(ATTR_ENTRY_ID)
        
    #     # Get command from CLI command text entity
    #     cli_command_entity = hass.states.get("text.meshcore_cli_command")
    #     if not cli_command_entity:
    #         _LOGGER.error("CLI command input helper not found: text.meshcore_cli_command")
    #         return
            
    #     command = cli_command_entity.state
        
    #     if not command:
    #         _LOGGER.warning("No command to execute - CLI input is empty")
    #         return
        
    #     # Create CLI command service call
    #     cli_call = create_service_call(
    #         DOMAIN, 
    #         SERVICE_CLI_COMMAND, 
    #         {"command": command, "entry_id": entry_id}
    #     )
        
    #     # Execute the CLI command
    #     await async_cli_command_service(cli_call)
        
    #     # Clear the command input after execution
    #     try:
    #         await hass.services.async_call(
    #             "text", 
    #             "set_value", 
    #             {"entity_id": "text.meshcore_cli_command", "value": ""},
    #             blocking=False
    #         )
    #     except Exception as ex:
    #         _LOGGER.warning(f"Could not clear CLI command input: {ex}")
    
    # Register the CLI command execution service
    # hass.services.async_register(
    #     DOMAIN,
    #     SERVICE_EXECUTE_CLI_COMMAND_UI,
    #     async_execute_cli_command_ui,
    #     schema=UI_MESSAGE_SCHEMA,
    # )

async def async_unload_services(hass: HomeAssistant) -> None:
    """Unload MeshCore services."""
    if hass.services.has_service(DOMAIN, SERVICE_SEND_MESSAGE):
        hass.services.async_remove(DOMAIN, SERVICE_SEND_MESSAGE)
        
    if hass.services.has_service(DOMAIN, SERVICE_SEND_CHANNEL_MESSAGE):
        hass.services.async_remove(DOMAIN, SERVICE_SEND_CHANNEL_MESSAGE)
        
    # if hass.services.has_service(DOMAIN, SERVICE_CLI_COMMAND):
    #     hass.services.async_remove(DOMAIN, SERVICE_CLI_COMMAND)
        
    if hass.services.has_service(DOMAIN, SERVICE_MESSAGE_SCRIPT):
        hass.services.async_remove(DOMAIN, SERVICE_MESSAGE_SCRIPT)
        
    # if hass.services.has_service(DOMAIN, SERVICE_EXECUTE_CLI_COMMAND_UI):
    #     hass.services.async_remove(DOMAIN, SERVICE_EXECUTE_CLI_COMMAND_UI)


def create_service_call(
    domain: str,
    service: str,
    data: Optional[Dict[str, Any]] = None,
    hass: Optional[HomeAssistant] = None
) -> ServiceCall:
    """Returns a ServiceCall instance compatible with the current Home Assistant version.
    
    In Home Assistant 2025.x.x and newer, ServiceCall requires a 'hass' parameter
    as the first argument, which was a breaking change from 2024.x.x. This factory
    function creates the appropriate instance based on the detected version.
    
    Args:
        domain: Service domain name
        service: Service name to call
        data: Dictionary containing service call parameters
        hass: HomeAssistant instance, required for 2025.x.x+ but ignored in 2024.x.x
        
    Returns:
        ServiceCall: Properly configured service call instance for the current HA version
    
    Example:
        # Create a service call that works across HA versions
        service_call = create_service_call(
            domain="light",
            service="turn_on",
            data={"entity_id": "light.living_room", "brightness": 255},
            hass=hass  # Always pass this, will be used only when needed
        )
    """
    # Create the service call instance based on the detected HA version
    if MAJOR_VERSION >= 2025:
        _LOGGER.debug("Creating ServiceCall with hass parameter (2025.x.x+ format)")
        return ServiceCall(
            hass=hass,
            domain=domain,
            service=service,
            data=data or {}
        )
    else:
        _LOGGER.debug("Creating ServiceCall without hass parameter (2024.x.x format)")
        return ServiceCall(
            domain=domain,
            service=service,
            data=data or {}
        )