"""The MeshCore integration."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from datetime import timedelta
from typing import Any, Dict
import time
from meshcore.events import Event, EventType
from .const import CHANNEL_PREFIX, NodeType

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.components.http import StaticPathConfig

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

# Import entity classes directly to avoid circular imports
from .binary_sensor import MeshCoreMessageEntity, MeshCoreContactDiagnosticBinarySensor, MeshCoreRepeaterBinarySensor

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

    CONF_REPEATER_SUBSCRIPTIONS,
    DEFAULT_REPEATER_UPDATE_INTERVAL,
    CONF_INFO_INTERVAL,
    CONF_MESSAGES_INTERVAL,
    DEFAULT_INFO_INTERVAL,
    DEFAULT_MESSAGES_INTERVAL,
    NodeType,
)
from .meshcore_api import MeshCoreAPI
from .services import async_setup_services, async_unload_services
from .logbook import handle_log_message

_LOGGER = logging.getLogger(__name__)

# List of platforms to set up
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SELECT, Platform.TEXT]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up MeshCore from a config entry."""
    # Get configuration from entry
    connection_type = entry.data[CONF_CONNECTION_TYPE]
    
    # Create API instance based on connection type
    api_kwargs = {
        "hass": hass,
        "connection_type": connection_type
    }
    
    if CONF_USB_PATH in entry.data:
        api_kwargs["usb_path"] = entry.data[CONF_USB_PATH]
    if CONF_BAUDRATE in entry.data:
        api_kwargs["baudrate"] = entry.data[CONF_BAUDRATE]
    if CONF_BLE_ADDRESS in entry.data:
        api_kwargs["ble_address"] = entry.data[CONF_BLE_ADDRESS]
    if CONF_TCP_HOST in entry.data:
        api_kwargs["tcp_host"] = entry.data[CONF_TCP_HOST]
    if CONF_TCP_PORT in entry.data:
        api_kwargs["tcp_port"] = entry.data[CONF_TCP_PORT]
    
    # Initialize API
    api = MeshCoreAPI(**api_kwargs)
    
    # Get the messages interval for base update frequency
    # Check options first, then data, then use default
    messages_interval = entry.options.get(
        CONF_MESSAGES_INTERVAL, 
        entry.data.get(CONF_MESSAGES_INTERVAL, DEFAULT_MESSAGES_INTERVAL)
    )
    
    # Create update coordinator with the messages interval (fastest polling rate)
    coordinator = MeshCoreDataUpdateCoordinator(
        hass,
        _LOGGER,
        name=DOMAIN,
        update_interval=timedelta(seconds=messages_interval),
        api=api,
        config_entry=entry,
    )
    
    # Fetch initial data
    await coordinator.async_config_entry_first_refresh()
    
    # Store coordinator for this entry
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator
    
    # Set up all platforms for this device
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    # Register static paths for icons
    should_cache = False
    icons_path = Path(__file__).parent / "www" / "icons"
    
    await hass.http.async_register_static_paths([
        StaticPathConfig("/api/meshcore/static", str(icons_path), should_cache)
    ])
    
    # Set up services
    await async_setup_services(hass)
    
    # Register update listener for config entry updates
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    
    return True

async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options for a config entry."""
    # Reload the entry to apply the new options
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    # Remove entry from data
    if unload_ok and entry.entry_id in hass.data[DOMAIN]:
        # Get coordinator and clean up
        coordinator = hass.data[DOMAIN][entry.entry_id]
        
        # Remove any event listeners registered by the coordinator
        if hasattr(coordinator, "_remove_listeners"):
            for remove_listener in coordinator._remove_listeners:
                remove_listener()
                
        # Disconnect from the device
        await coordinator.api.disconnect()
        
        # Remove entry
        hass.data[DOMAIN].pop(entry.entry_id)
        
        # If no more entries, unload services
        if not hass.data[DOMAIN]:
            await async_unload_services(hass)
    
    return unload_ok


class MeshCoreDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the MeshCore node and trigger event-generating commands."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        name: str,
        update_interval: timedelta,
        api: MeshCoreAPI,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            logger,
            name=name,
            update_interval=update_interval,
        )     
        self.api = api
        self.config_entry = config_entry
        self.data: Dict[str, Any] = {}
        self._current_node_info = {}
        self._contacts = []
        # Get name and pubkey from config_entry.data (not options)
        self.name = config_entry.data.get(CONF_NAME)
        self.pubkey = config_entry.data.get(CONF_PUBKEY)
        
        # Set up device info that entities can reference
        self._firmware_version = None
        self._hardware_model = None
        
        # Create a central device_info dict that all entities can reference
        self.device_info = {
            "identifiers": {(DOMAIN, config_entry.entry_id)},
            "name": f"MeshCore {self.name or 'Node'} ({self.pubkey[:6] if self.pubkey else ''})",
            "manufacturer": "MeshCore",
            "model": "Mesh Radio",
            "sw_version": "Unknown",
        }

        # Single map to track all message timestamps (key -> timestamp)
        # Keys can be channel indices (int) or public key prefixes (str)
        self.message_timestamps = {}
        
        # Repeater subscription tracking
        self._repeater_stats = {}
        self._repeater_login_times = {}
        
        # Room server ping tracking - hourly interval
        self._roomserver_ping_times = {}
        self._roomserver_ping_interval = 3600  # 1 hour in seconds
        
        # Track connected state
        self._is_connected = False
        
        # Register listener for connection state changes
        if hass:
            self._remove_listeners = [
                hass.bus.async_listen(f"{DOMAIN}_connected", self._handle_connected),
                hass.bus.async_listen(f"{DOMAIN}_disconnected", self._handle_disconnected)
            ]
            
        # Initialize tracking sets for entities
        self.tracked_contacts = set()
        self.tracked_diagnostic_binary_contacts = set()
        self.channels_added = False
        
        # Track last update times for different data types
        self._last_info_update = 0  # Combined time for node info and contacts
        self._last_messages_update = 0
        self._last_repeater_updates = {}  # Dictionary to track per-repeater updates
        self._contacts = []
        self._last_contacts_update = 0
        
        # Get interval settings from config (or use defaults)
        self._info_interval = config_entry.options.get(
            CONF_INFO_INTERVAL, DEFAULT_INFO_INTERVAL
        )
        self._messages_interval = config_entry.options.get(
            CONF_MESSAGES_INTERVAL, DEFAULT_MESSAGES_INTERVAL
        )
        
        if not hasattr(self, "last_update_success_time"):
            self.last_update_success_time = time.time()
        
    async def _handle_connected(self, event):
        """Handle connected event."""
        self._is_connected = True
        self.logger.info("MeshCore device connected")
        
    async def _handle_disconnected(self, event):
        """Handle disconnected event."""
        self._is_connected = False
        self.logger.info("MeshCore device disconnected")
        
    async def _fetch_repeater_stats(self, result_data: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch stats from configured repeaters based on their individual update intervals."""
        if not self.config_entry:
            self.logger.debug("No config entry available, skipping repeater stats")
            return result_data
            
        # Get repeater subscriptions from config entry
        repeater_subscriptions = self.config_entry.data.get(CONF_REPEATER_SUBSCRIPTIONS, [])
        if not repeater_subscriptions:
            self.logger.debug("No repeater subscriptions configured, skipping repeater stats")
            return result_data
            
        self.logger.debug(f"Found {len(repeater_subscriptions)} repeater subscriptions to check")
            
        # Create a dictionary to store all repeater stats (including cached ones)
        all_repeater_stats = {}
        
        # Initialize repeater versions if needed
        if not hasattr(self, "_repeater_versions"):
            self._repeater_versions = {}
            # Also initialize version check timestamps
            self._last_version_checks = {}
            
        # Start with any existing stats we have
        if hasattr(self, "_repeater_stats") and self._repeater_stats:
            all_repeater_stats.update(self._repeater_stats)
        
        # Current time for interval calculations
        current_time = time.time()
        
        # Process each repeater subscription
        for repeater in repeater_subscriptions:
            # Skip disabled repeaters
            if not repeater.get("enabled", True):
                self.logger.debug(f"Skipping disabled repeater: {repeater.get('name')}")
                continue
                
            repeater_name = repeater.get("name")
            password = repeater.get("password", "")
            update_interval = repeater.get("update_interval", DEFAULT_REPEATER_UPDATE_INTERVAL)
            
            if not repeater_name:
                self.logger.warning(f"Skipping repeater with missing name: {repeater}")
                continue
                
            # Check if it's time to update this repeater based on its interval
            last_update = self._last_repeater_updates.get(repeater_name, 0)
            time_since_update = current_time - last_update
            
            # Skip update if not enough time has passed
            if time_since_update < update_interval and last_update > 0:
                self.logger.debug(
                    f"Skipping repeater {repeater_name} update - " +
                    f"last update was {time_since_update:.1f}s ago (interval: {update_interval}s)"
                )
                continue
                
            self.logger.info(
                f"Updating repeater {repeater_name} after {time_since_update:.1f}s " +
                f"(interval: {update_interval}s)"
            )
                
            # Check if we need to re-login (login times tracked per repeater)
            last_login_time = self._repeater_login_times.get(repeater_name, 0)
            # Re-login every hour (3600 seconds)
            login_interval = 3600
            time_since_login = current_time - last_login_time
            need_login = time_since_login > login_interval
            
            if need_login:
                self.logger.info(f"Login needed for {repeater_name} - last login was {time_since_login:.1f}s ago (limit: {login_interval}s)")
                # Password can be empty for guest login
                login_success = await self.api.login_to_repeater(repeater_name, password)
                
                if login_success:
                    self._repeater_login_times[repeater_name] = current_time
                    self.logger.info(f"Successfully logged in to repeater: {repeater_name}")
                    
                    # Only check version if a password was provided (admin permissions)
                    if password:
                        # Check if we need to fetch repeater version (on first login or daily)
                        # Daily version check (86400 seconds = 24 hours)
                        version_check_interval = 86400
                        last_version_check = self._last_version_checks.get(repeater_name, 0)
                        time_since_version_check = current_time - last_version_check
                        
                        # Get version on first login or if it's been more than a day
                        if repeater_name not in self._repeater_versions or time_since_version_check >= version_check_interval:
                            self.logger.info(f"Fetching version for repeater {repeater_name} (admin login)")
                            version = await self.api.get_repeater_version(repeater_name)
                            
                            if version:
                                self._repeater_versions[repeater_name] = version
                                self.logger.info(f"Updated version for repeater {repeater_name}: {version}")
                            else:
                                self.logger.warning(f"Could not get version for repeater {repeater_name}")
                                
                            # Update timestamp even on failure to avoid constant retries
                            self._last_version_checks[repeater_name] = current_time
                    else:
                        self.logger.debug(f"Skipping version check for repeater {repeater_name} (no admin password provided)")
                
                    # Check if this repeater is also a room server
                    is_roomserver = False
                    for contact_name, contact in self.api._cached_contacts.items():
                        if contact_name == repeater_name:
                            # Check if node type indicates it's a room server
                            if contact.get("type") == NodeType.ROOM_SERVER:
                                is_roomserver = True
                                break
                    
                    # If this is a room server, check if we need to send a ping
                    if is_roomserver:
                        last_ping_time = self._roomserver_ping_times.get(repeater_name, 0)
                        time_since_ping = current_time - last_ping_time
                        
                        # Only ping if the room server ping interval has passed
                        if time_since_ping >= self._roomserver_ping_interval:
                            self.logger.info(f"Sending room server ping to {repeater_name} (after {time_since_ping:.1f}s)")
                            await self.api.roomserver_ping(repeater_name)
                            self._roomserver_ping_times[repeater_name] = current_time
                        else:
                            self.logger.debug(f"Skipping room server ping to {repeater_name} - last ping was {time_since_ping:.1f}s ago")
                else:
                    self.logger.error(f"Failed to login to repeater: {repeater_name} - using password: {'yes' if password else 'no (guest)'}")
            else:
                self.logger.debug(f"No login needed for {repeater_name} - last login was {time_since_login:.1f}s ago")
            
            try:
                self.logger.info(f"Fetching stats from repeater: {repeater_name}")
                stats = await self.api.get_repeater_stats(repeater_name)
                
                if stats:
                    # Always add repeater_name and public_key to stats
                    stats["repeater_name"] = repeater_name
                    
                    # Find and add public key
                    for contact_name, contact in self.api._cached_contacts.items():
                        if contact_name == repeater_name:
                            public_key = contact.get("public_key", "")
                            stats["public_key"] = public_key
                            # Also add a shortened version for display
                            stats["public_key_short"] = public_key[:10] if public_key else ""
                            break
                    
                    # Add version info if available
                    if repeater_name in self._repeater_versions:
                        stats["version"] = self._repeater_versions[repeater_name]
                    
                    # Add the stats to our results
                    self._repeater_stats[repeater_name] = stats
                    all_repeater_stats[repeater_name] = stats
                    self.logger.info(f"Successfully updated stats for repeater: {repeater_name}")
                else:
                    self.logger.warning(f"No stats received for repeater: {repeater_name}")
            except Exception as ex:
                self.logger.error(f"Error fetching stats for repeater {repeater_name}: {ex}")
            
            self._last_repeater_updates[repeater_name] = current_time
        
        # Add all repeater stats to the result data
        if all_repeater_stats:
            result_data["repeater_stats"] = all_repeater_stats
            self.logger.debug(f"Added stats for {len(all_repeater_stats)} repeaters to result data")
            
        return result_data
    
    async def _trigger_repeater_updates(self, mesh_core):
        """Trigger commands for repeater updates without waiting for responses."""
        if not self.config_entry:
            return
            
        # Get repeater subscriptions from config entry
        repeater_subscriptions = self.config_entry.data.get(CONF_REPEATER_SUBSCRIPTIONS, [])
        if not repeater_subscriptions:
            return
            
        current_time = time.time()
        
        # Process each repeater subscription
        for repeater in repeater_subscriptions:
            # Skip disabled repeaters
            if not repeater.get("enabled", True):
                continue
                
            repeater_name = repeater.get("name")
            password = repeater.get("password", "")
            update_interval = repeater.get("update_interval", DEFAULT_REPEATER_UPDATE_INTERVAL)
            
            if not repeater_name:
                continue
                
            # Check if it's time to update this repeater
            last_update = self._last_repeater_updates.get(repeater_name, 0)
            time_since_update = current_time - last_update
            
            # Skip if not enough time has passed
            if time_since_update < update_interval and last_update > 0:
                continue
                
            # Find the repeater in contacts list to get public key
            pubkey_prefix = None
            
            for contact in self._contacts:
                if contact.get("adv_name") == repeater_name and "public_key" in contact:
                    public_key = contact["public_key"]
                    pubkey_prefix = bytes.fromhex(public_key)[:6]
                    break
                    
            if not pubkey_prefix:
                self.logger.warning(f"Cannot find public key for repeater {repeater_name}")
                continue
                
            try:
                # Check if we need to login
                last_login = self._repeater_login_times.get(repeater_name, 0)
                time_since_login = current_time - last_login
                login_interval = 3600  # 1 hour
                
                # Send login command if needed - response will be handled by subscribed entities
                if time_since_login > login_interval:
                    self.logger.info(f"Sending login command to repeater {repeater_name}")
                    await mesh_core.send_login(pubkey_prefix, password if password else "")
                    self._repeater_login_times[repeater_name] = current_time
                
                # Send status request - response will be handled by subscribed entities
                if time_since_update > update_interval:
                    self.logger.info(f"Requesting stats from repeater {repeater_name}")
                    await mesh_core.send_statusreq(pubkey_prefix)
                    self._last_repeater_updates[repeater_name] = current_time
                    
            except Exception as ex:
                self.logger.error(f"Error sending commands to repeater {repeater_name}: {ex}")
                
                
    # function just looks at update intervals and triggers commands
    async def _async_update_data(self) -> None:
        """Trigger commands that will generate events on schedule.
        
        In the event-driven architecture, this method:
        1. Ensures we're connected to the device
        2. Triggers commands based on scheduled intervals
        3. Maintains shared data like contacts list
        
        The actual state updates happen through event subscriptions in the entities.
        """
        # Initialize result with previous data
        result_data = dict(self.data) if self.data else {
            "name": "MeshCore Node", 
            "contacts": []
        }
        
        current_time = time.time()
    
        # Reconnect if needed
        if not self.api.connected:
            self.logger.info("Connecting to device...")
            await self.api.disconnect()
            connection_success = await self.api.connect()
            if not connection_success:
                self.logger.error("Failed to connect to MeshCore device")
                raise UpdateFailed("Failed to connect to MeshCore device")

        # Always get the basics
        await self.api.mesh_core.commands.send_appstart()
        await self.api.mesh_core.commands.get_bat()
        
        # Fetch device info if we don't have it yet
        if self._firmware_version is None or self._hardware_model is None:
            try:
                self.logger.info("Fetching device info...")
                device_query_result = await self.api.mesh_core.commands.send_device_query()
                if device_query_result.type is EventType.DEVICE_INFO:
                    self._firmware_version = device_query_result.payload.get("ver")
                    self._hardware_model = device_query_result.payload.get("model")
                    
                    if self._firmware_version:
                        self.device_info["sw_version"] = self._firmware_version
                    if self._hardware_model:
                        self.device_info["model"] = self._hardware_model
                        
                    self.logger.info(f"Device info updated - Firmware: {self._firmware_version}, Model: {self._hardware_model}")
                    
                    self.async_update_listeners()
            except Exception as ex:
                self.logger.error(f"Error fetching device info: {ex}")
        
        # Get contacts - no need to process, just store in data
        contacts_result = await self.api.mesh_core.commands.get_contacts()
        
        # Convert contacts to list and store
        if contacts_result and hasattr(contacts_result, "payload"):
            self._contacts = list(contacts_result.payload.values())
            
        # Store contacts in result data
        result_data["contacts"] = self._contacts

        # Check for messages
        _LOGGER.info("Clearing message queue...")
        try:
            res = True
            while res:
                result = await self.api.mesh_core.commands.get_msg()
                if result.type == EventType.NO_MORE_MSGS:
                    res = False
                    _LOGGER.debug("No more messages in queue")
                elif result.type == EventType.ERROR:
                    res = False
                    _LOGGER.error(f"Error retrieving messages: {result.payload}")
                else:
                    _LOGGER.debug(f"Cleared message: {result}")
        except Exception as ex:
            _LOGGER.error(f"Error clearing message queue: {ex}")
            
        
        # Set initial update times
        self._last_info_update = current_time
        self._last_contacts_update = current_time
        self._last_messages_update = current_time
        
        # Initialize repeater update tracking
        # if self.config_entry:
        #     repeaters = self.config_entry.data.get(CONF_REPEATER_SUBSCRIPTIONS, [])
        #     for repeater in repeaters:
        #         repeater_name = repeater.get("name")
        #         if repeater_name:
        #             self._last_repeater_updates[repeater_name] = current_time
                    
        # Trigger repeater updates
       #  await self._trigger_repeater_updates(self.api.mesh_core)
            # else:
            #     # Conditional command triggers based on intervals
                
            #     # 1. Always check for messages (base update interval)
            #     time_since_messages = current_time - self._last_messages_update
            #     self.logger.debug(f"Checking messages after {time_since_messages:.1f}s")
            #     await self._check_for_messages(self.api.mesh_core)
            #     self._last_messages_update = current_time
                
            #     # 2. Check battery and device info if interval has passed
            #     time_since_info = current_time - self._last_info_update
            #     if time_since_info >= self._info_interval:
            #         self.logger.debug(f"Updating device info after {time_since_info:.1f}s")
                    
            #         # Update app session to refresh self info
            #         await mesh_core.send_appstart()
                    
            #         # Request battery update
            #         await mesh_core.get_bat()
                    
            #         # Store mesh_core's self_info in our data
            #         if hasattr(mesh_core, "self_info") and mesh_core.self_info:
            #             for key, value in mesh_core.self_info.items():
            #                 result_data[key] = value
                            
            #         self._last_info_update = current_time
                
            #     # 3. Check contacts if interval has passed
            #     time_since_contacts = current_time - self._last_contacts_update
            #     if time_since_contacts >= self._info_interval:
            #         self.logger.debug(f"Updating contacts after {time_since_contacts:.1f}s")
                    
            #         # Get contacts
            #         contacts = await mesh_core.get_contacts()
            #         if contacts:
            #             # Process contacts update
            #             await self._process_contacts_update(contacts)
            #             result_data["contacts"] = self._contacts
                        
            #         self._last_contacts_update = current_time
            #     else:
            #         # Use cached contacts
            #         result_data["contacts"] = self._contacts
                
            #     # 4. Trigger repeater updates as needed
            #     await self._trigger_repeater_updates(mesh_core)
            
            # # Always update last_update_success_time 
            # self.last_update_success_time = current_time
            
            # return result_data
            
        # except Exception as err:
        #     self.logger.error(f"Error during update: {err}")
        #     self.logger.exception("Detailed exception")
            
        #     # If we have previous data, return that instead of failing
        #     if self.data:
        #         return self.data
                
        #     # Minimal fallback data
        #     return {
        #         "name": "MeshCore Node",
        #         "contacts": self._contacts
        #     }