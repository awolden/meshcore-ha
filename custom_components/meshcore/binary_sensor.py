"""Binary sensor platform for MeshCore integration."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from meshcore.events import Event, EventType

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import (
    DOMAIN,
    ENTITY_DOMAIN_BINARY_SENSOR,
    MESSAGES_SUFFIX,
    CHANNEL_PREFIX,
    CONTACT_SUFFIX,
    NodeType,
)
from .utils import (
    get_device_key,
    sanitize_name,
    format_entity_id,
    extract_channel_idx,
)
from .logbook import handle_log_message

_LOGGER = logging.getLogger(__name__)

# How far back to check for messages (2 weeks, matching activity window)
MESSAGE_ACTIVITY_WINDOW = timedelta(days=14)

@callback
def handle_contacts_update(event, coordinator, async_add_entities):
    """Process contacts update from mesh_core."""
    if not event or not hasattr(event, "payload") or not event.payload:
        return
        
    # Initialize tracking sets if needed
    if not hasattr(coordinator, "tracked_contacts"):
        coordinator.tracked_contacts = set()
    if not hasattr(coordinator, "tracked_diagnostic_binary_contacts"):
        coordinator.tracked_diagnostic_binary_contacts = set()
    
    contact_entities = []
    
    # Process each contact in the event payload
    for key, contact in event.payload.items():
        if not isinstance(contact, dict):
            continue
            
        contact_name = contact.get("adv_name", "Unknown")
        public_key = contact.get("public_key", "")
        
        # Create diagnostic binary sensor
        if public_key and public_key not in coordinator.tracked_diagnostic_binary_contacts:
            try:
                coordinator.tracked_diagnostic_binary_contacts.add(public_key)
                contact_entities.append(MeshCoreContactDiagnosticBinarySensor(
                    coordinator, 
                    contact_name,
                    public_key,
                    public_key[:12]
                ))
            except Exception as ex:
                _LOGGER.error(f"Error setting up contact diagnostic binary sensor: {ex}")
    
    # Add new entities
    if contact_entities:
        _LOGGER.info(f"Adding {len(contact_entities)} diagnostic entities from CONTACTS event")
        async_add_entities(contact_entities)

@callback
def handle_contact_message(event, coordinator, async_add_entities):
    print(f"Received contact message event: {event}")
    """Create message entity on first message received from a contact."""
    if not event or not hasattr(event, "payload") or not event.payload:
        return
        
    # Skip if we don't have meshcore
    if not coordinator.api.mesh_core:
        return
    
    # Extract pubkey_prefix from the event payload
    payload = event.payload
    pubkey_prefix = payload.get("pubkey_prefix")
    
    # Skip if no pubkey_prefix or already tracking this contact
    if not pubkey_prefix or pubkey_prefix in coordinator.tracked_contacts:
        return
    
    # Get contact information from MeshCore
    contact = coordinator.api.mesh_core.get_contact_by_key_prefix(pubkey_prefix)
    if not contact:
        return
        
    contact_name = contact.get("adv_name", "Unknown")
    
    # Create message entity for this contact
    message_entity = MeshCoreMessageEntity(
        coordinator, pubkey_prefix, f"{contact_name} Messages", 
        public_key=pubkey_prefix
    )
    
    # Track this contact
    coordinator.tracked_contacts.add(pubkey_prefix)
    
    # Add the entity
    _LOGGER.info(f"Adding message entity for {contact_name} after receiving message")
    async_add_entities([message_entity])

@callback
def handle_channel_message(event, coordinator, async_add_entities):
    print(f"Received channel message event: {event}")
    """Create channel message entity on first message in a channel."""
    if not event or not hasattr(event, "payload") or not event.payload:
        return
        
    # Skip if we don't have meshcore
    if not coordinator.api.mesh_core:
        return
    
    # Extract channel_idx from the event payload
    payload = event.payload
    channel_idx = payload.get("channel_idx")
    
    # Skip if no channel_idx or if channels are already added
    if channel_idx is None or hasattr(coordinator, "channels_added") and coordinator.channels_added:
        return
    
    # Initialize channels list if needed
    if not hasattr(coordinator, "tracked_channels"):
        coordinator.tracked_channels = set()
    
    # Skip if this channel is already tracked
    if channel_idx in coordinator.tracked_channels:
        return
        
    # Create channel entity
    safe_channel = f"{CHANNEL_PREFIX}{channel_idx}"
    channel_entity = MeshCoreMessageEntity(
        coordinator, safe_channel, f"Channel {channel_idx} Messages"
    )
    
    # Track this channel
    coordinator.tracked_channels.add(channel_idx)
    
    # Add the entity
    _LOGGER.info(f"Adding message entity for channel {channel_idx} after receiving message")
    async_add_entities([channel_entity])

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up MeshCore message entities from config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    
    # Initialize tracking sets
    coordinator.tracked_contacts = set()
    coordinator.tracked_channels = set()
    coordinator.tracked_diagnostic_binary_contacts = set()
    
    # Create repeater entities if configured
    repeater_entities = []
    repeater_subscriptions = entry.data.get("repeater_subscriptions", [])
    for repeater in repeater_subscriptions:
        if not repeater.get("enabled", True):
            continue
            
        repeater_name = repeater.get("name")
        if not repeater_name:
            continue
            
        repeater_entities.append(MeshCoreRepeaterBinarySensor(
            coordinator, repeater_name, "status"
        ))
    
    # Add repeater entities
    if repeater_entities:
        async_add_entities(repeater_entities)
    
    # Set up event listeners
    listeners = []
    
    # Create event handlers
    @callback
    def contacts_event_handler(event):
        handle_contacts_update(event, coordinator, async_add_entities)
    
    @callback
    def contact_message_handler(event):
        handle_contact_message(event, coordinator, async_add_entities)
        
    @callback
    def channel_message_handler(event):
        handle_channel_message(event, coordinator, async_add_entities)
    
    # Subscribe to events directly from mesh_core
    if coordinator.api.mesh_core:
        # Contact discovery for diagnostic entities
        listeners.append(coordinator.api.mesh_core.subscribe(
            EventType.CONTACTS,
            contacts_event_handler
        ))
        
        # Message events to create entities on first message
        listeners.append(coordinator.api.mesh_core.subscribe(
            EventType.CONTACT_MSG_RECV,
            contact_message_handler
        ))
        
        # Channel message events
        listeners.append(coordinator.api.mesh_core.subscribe(
            EventType.CHANNEL_MSG_RECV,
            channel_message_handler
        ))
    

class MeshCoreMessageEntity(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor entity that tracks mesh network messages using event subscription."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    
    @property
    def state(self) -> str:
        """Return the state of the entity."""
        return "Active" if self.is_on else "Inactive"
    
    def __init__(
        self, 
        coordinator: DataUpdateCoordinator, 
        entity_key: str,
        name: str,
        public_key: str = ""
    ) -> None:
        """Initialize the message entity."""
        super().__init__(coordinator)
        
        # Store entity type and public key if applicable
        self.entity_key = entity_key
        self.public_key = public_key
        
        # Get device name for unique ID and entity_id
        device_key = get_device_key(coordinator)
        
        # Set unique ID with device key included - ensure consistent format with no empty parts
        parts = [part for part in [coordinator.config_entry.entry_id, device_key[:6], entity_key[:6], MESSAGES_SUFFIX] if part]
        self._attr_unique_id = "_".join(parts)
        
        # Manually set entity_id to match logbook entity_id format
        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_BINARY_SENSOR, 
            device_key[:6], 
            entity_key[:6], 
            MESSAGES_SUFFIX
        )
        
        # Debug: Log the entity ID for troubleshooting
        _LOGGER.debug(f"Created entity with ID: {self.entity_id}")
        
        self._attr_name = name
        
        # Set icon based on entity type
        if self.entity_key.startswith(CHANNEL_PREFIX):
            self._attr_icon = "mdi:message-bulleted"
        else:
            self._attr_icon = "mdi:message-text-outline"
        
        
        # Initialize tracking variables
        self._last_message_time = 0
        self._remove_channel_listener = None
        self._remove_contact_listener = None
        
    async def async_added_to_hass(self):
        """Register event handlers when entity is added to hass."""
        await super().async_added_to_hass()
        
        # Only set up listeners if a MeshCore instance is available
        if not self.coordinator.api.mesh_core:
            _LOGGER.warning("No MeshCore instance available for subscriptions")
            return
            
        mesh_core = self.coordinator.api.mesh_core
        
        # Set up subscription based on entity type
        # Use importlib to avoid confusion with local 'meshcore' module
        import importlib
        meshcore_events = importlib.import_module('meshcore.events')
        EventType = meshcore_events.EventType
        
        if self.entity_key.startswith(CHANNEL_PREFIX):
            # For channel messages, subscribe with channel filter
            try:
                channel_idx = extract_channel_idx(self.entity_key)
                _LOGGER.info(f"Subscribing to channel {channel_idx} messages")
                
                self._remove_channel_listener = mesh_core.subscribe(
                    EventType.CHANNEL_MSG_RECV,
                    self._handle_message_event,
                    {"channel_idx": channel_idx}  # Filter by channel
                )
            except Exception as ex:
                _LOGGER.error(f"Error setting up channel message subscription: {ex}")
                
        elif self.public_key:
            # For contact messages, subscribe with pubkey_prefix filter
            try:
                pubkey_prefix = self.public_key[:12]
                _LOGGER.info(f"Subscribing to contact {pubkey_prefix} messages")
                
                self._remove_contact_listener = mesh_core.subscribe(
                    EventType.CONTACT_MSG_RECV,
                    self._handle_message_event,
                    {"pubkey_prefix": pubkey_prefix}  # Filter by pubkey_prefix
                )
            except Exception as ex:
                _LOGGER.error(f"Error setting up contact message subscription: {ex}")
        
    async def async_will_remove_from_hass(self):
        """Clean up subscriptions when entity is removed."""
        # Unsubscribe from events
        if self._remove_channel_listener:
            try:
                self._remove_channel_listener()
                self._remove_channel_listener = None
            except Exception as ex:
                _LOGGER.error(f"Error removing channel listener: {ex}")
                
        if self._remove_contact_listener:
            try:
                self._remove_contact_listener()
                self._remove_contact_listener = None
            except Exception as ex:
                _LOGGER.error(f"Error removing contact listener: {ex}")
                
        await super().async_will_remove_from_hass()
    
    async def _handle_message_event(self, event):
        """Handle message events."""
        if not event or not hasattr(event, "payload"):
            return
            
        _LOGGER.debug(f"Received message event: {event}")
        
        # Update timestamp
        self._last_message_time = time.time()
        
        # For backward compatibility, also update coordinator's timestamps
        if not hasattr(self.coordinator, "message_timestamps"):
            self.coordinator.message_timestamps = {}
            
        # Determine key to use in coordinator timestamps
        key = None
        if self.entity_key.startswith(CHANNEL_PREFIX):
            key = extract_channel_idx(self.entity_key)
        elif self.public_key:
            key = self.public_key
            
        if key is not None:
            self.coordinator.message_timestamps[key] = self._last_message_time
        
        # Log message to logbook
        payload = event.payload
        if isinstance(payload, dict):
            handle_log_message(self.hass, payload)
        
        # Update entity state
        self.async_write_ha_state()
                
    
    @property
    def device_info(self):
        return DeviceInfo(**self.coordinator.device_info)
        
    @property
    def is_on(self) -> bool:
        """Return true if there are recent messages in the activity window."""
        return True
    
    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return message details as attributes."""
        attributes = {
            "last_updated": datetime.now().isoformat()
        }
        
        # Add appropriate attributes based on entity type
        if self.entity_key.startswith(CHANNEL_PREFIX):
            # For channel-specific message entities
            try:
                channel_idx = extract_channel_idx(self.entity_key)
                attributes["channel_index"] = f"{channel_idx}"
            except (ValueError, TypeError):
                _LOGGER.warning(f"Could not get channel index from {self.entity_key}")
        elif self.public_key:
            # For contact-specific message entities
            attributes["public_key"] = self.public_key
            
        # Add timestamp of last message if available
        if self._last_message_time > 0:
            attributes["last_message"] = datetime.fromtimestamp(self._last_message_time).isoformat()
            
        return attributes


class MeshCoreContactDiagnosticBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """A diagnostic binary sensor for a single MeshCore contact."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        contact_name: str,
        public_key: str,
        contact_id: str,
    ) -> None:
        """Initialize the contact diagnostic binary sensor."""
        super().__init__(coordinator)
        
        self.contact_name = contact_name
        self.public_key = public_key
        self.pubkey_prefix = public_key[:12] if public_key else ""
        self._contact_data = {}
        self._remove_contacts_listener = None
        
        # Set unique ID
        self._attr_unique_id = contact_id
        
        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_BINARY_SENSOR,
            contact_name,
            self.pubkey_prefix,
            CONTACT_SUFFIX
        )

        # Initial name
        self._attr_name = contact_name
        
        # Set entity category to diagnostic
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        
        # Set device class to connectivity
        self._attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
        
        # Icon will be set dynamically
        self._attr_icon = "mdi:radio-tower"
        
        # Get initial data from coordinator
        initial_data = self._get_contact_data()
        if initial_data:
            self._update_from_contact_data(initial_data)

    async def async_added_to_hass(self):
        """Register contact events when added to hass."""
        await super().async_added_to_hass()
        
        # Only set up listeners if a MeshCore instance is available
        if not self.coordinator.api.mesh_core:
            return
        
        # Import here to avoid import errors
        import importlib
        meshcore_events = importlib.import_module('meshcore.events')
        EventType = meshcore_events.EventType
        
        try:
            # Subscribe to contacts update events
            self._remove_contacts_listener = self.coordinator.api.mesh_core.subscribe(
                EventType.CONTACTS, 
                self._handle_contacts_event
            )
        except Exception as ex:
            _LOGGER.error(f"Error setting up contacts event subscription: {ex}")
    
    async def async_will_remove_from_hass(self):
        """Clean up subscriptions when entity is removed."""           
        if self._remove_contacts_listener:
            try:
                self._remove_contacts_listener()
                self._remove_contacts_listener = None
            except Exception as ex:
                _LOGGER.error(f"Error removing contacts listener: {ex}")
        
        await super().async_will_remove_from_hass()
    
    async def _handle_contacts_event(self, event):
        """Handle contacts update events."""
        if not event or not hasattr(event, "payload") or not event.payload:
            return
            
        # Look for our contact in the contacts list
        for contact in event.payload.values():
            if not isinstance(contact, dict):
                continue
                
            # Match by public key
            if contact.get("public_key", "").startswith(self.public_key):
                self._update_from_contact_data(contact)
                self.async_write_ha_state()
                break
                
            # Match by name (fallback)
            if contact.get("adv_name") == self.contact_name:
                self._update_from_contact_data(contact)
                self.async_write_ha_state()
                break

    @property
    def device_info(self):
        return DeviceInfo(**self.coordinator.device_info)
        
    def _get_contact_data(self) -> Dict[str, Any]:
        """Get the data for this contact from the coordinator."""
        if not self.coordinator.data or not isinstance(self.coordinator.data, dict):
            return {}
            
        contacts = self.coordinator.data.get("contacts", [])
        if not contacts:
            return {}
            
        # Find this contact by name or by public key
        for contact in contacts:
            if not isinstance(contact, dict):
                continue
                
            # Match by public key prefix
            if contact.get("public_key", "").startswith(self.public_key):
                return contact

            # Match by name
            if contact.get("adv_name") == self.contact_name:
                return contact
                
        return {}
    
    def _update_from_contact_data(self, contact: Dict[str, Any]):
        """Update entity state based on contact data."""
        if not contact:
            return
        
        # Store the contact data
        self._contact_data = dict(contact)
        
        # Get the node type and set icon accordingly
        node_type = contact.get("type")
        is_fresh = self.is_on
        
        # Set different icons and names based on node type and state
        if node_type == NodeType.CLIENT:  # Client
            self._attr_icon = "mdi:account" if is_fresh else "mdi:account-off"
            self._attr_name = f"{self.contact_name} (Client)"
        elif node_type == NodeType.REPEATER:  # Repeater
            self._attr_icon = "mdi:radio-tower" if is_fresh else "mdi:radio-tower-off"
            self._attr_name = f"{self.contact_name} (Repeater)"
        elif node_type == NodeType.ROOM_SERVER:  # Room Server
            self._attr_icon = "mdi:forum" if is_fresh else "mdi:forum-outline"
            self._attr_name = f"{self.contact_name} (Room Server)"
        else:
            # Default icon if type is unknown
            self._attr_icon = "mdi:help-network"
            self._attr_name = f"{self.contact_name} (Unknown)"

    @property
    def is_on(self) -> bool:
        """Return True if the contact is fresh/active."""
        if not self._contact_data:
            return False
            
        # Check last advertisement time for contact status
        last_advert = self._contact_data.get("last_advert", 0)
        if last_advert > 0:
            # Calculate time since last advert
            time_since = time.time() - last_advert
            # If less than 12 hour, consider fresh/active
            if time_since < 3600*12:
                return True
        
        return False
        
    @property
    def state(self) -> str:
        """Return the state of the binary sensor as "fresh" or "stale"."""
        return "fresh" if self.is_on else "stale"
        
    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return the contact data as attributes."""
        if not self._contact_data:
            return {"status": "unknown"}
            
        attributes = {}
        
        # Add all contact properties as attributes
        for key, value in self._contact_data.items():
            attributes[key] = value
        
        # Get node type string
        node_type = self._contact_data.get("type")
        if node_type == NodeType.CLIENT:
            attributes["node_type_str"] = "Client"
            icon_file = "client-green.svg" if self.is_on else "client.svg"
        elif node_type == NodeType.REPEATER:
            attributes["node_type_str"] = "Repeater"
            icon_file = "repeater-green.svg" if self.is_on else "repeater.svg"
        elif node_type == NodeType.ROOM_SERVER:
            attributes["node_type_str"] = "Room Server"
            icon_file = "room_server-green.svg" if self.is_on else "room_server.svg"
        else:
            attributes["node_type_str"] = "Unknown"
            icon_file = None
            
        # Add entity picture if we have an icon
        if icon_file:
            attributes["entity_picture"] = f"/api/meshcore/static/{icon_file}"
        
        # Format last advertisement time if available
        last_advert = self._contact_data.get("last_advert", 0)
        if last_advert > 0:
            last_advert_time = datetime.fromtimestamp(last_advert)
            attributes["last_advert_formatted"] = last_advert_time.isoformat()
            
        return attributes


class MeshCoreRepeaterBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor for repeater status."""
    
    def __init__(
        self, 
        coordinator: DataUpdateCoordinator,
        repeater_name: str,
        stat_key: str,
    ) -> None:
        """Initialize the repeater binary sensor."""
        super().__init__(coordinator)
        self.repeater_name = repeater_name
        self.stat_key = stat_key
        
        # Create sanitized names
        safe_name = sanitize_name(repeater_name)
        
        # Generate a unique device_id for this repeater
        self.device_id = f"{coordinator.config_entry.entry_id}_repeater_{safe_name}"
        
        # Set unique ID
        self._attr_unique_id = f"{self.device_id}_{stat_key}"
        
        # Set friendly name
        self._attr_name = f"{stat_key.replace('_', ' ').title()}"
        
        # Set device class to connectivity
        self._attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
        
        # Set entity ID
        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_BINARY_SENSOR,
            safe_name,
            stat_key
        )
        
        # Get repeater stats if available
        repeater_stats = coordinator.data.get("repeater_stats", {}).get(repeater_name, {})
        
        # Default device name, include public key if available
        device_name = f"MeshCore Repeater: {repeater_name}"
        if repeater_stats and "public_key" in repeater_stats:
            public_key_short = repeater_stats.get("public_key_short", repeater_stats["public_key"][:10])
            device_name = f"MeshCore Repeater: {repeater_name} ({public_key_short})"
        
        # Set device info to create a separate device for this repeater
        device_info = {
            "identifiers": {(DOMAIN, self.device_id)},
            "name": device_name,
            "manufacturer": repeater_stats.get("manufacturer_name", "MeshCore") if repeater_stats else "MeshCore",
            "model": "Mesh Repeater",
            "via_device": (DOMAIN, coordinator.config_entry.entry_id),  # Link to the main device
        }
        
        # Add version information if available
        if repeater_stats:
            # Prefer firmware_version if available, fall back to version
            if "firmware_version" in repeater_stats:
                device_info["sw_version"] = repeater_stats["firmware_version"]
            elif "version" in repeater_stats:
                device_info["sw_version"] = repeater_stats["version"]
                
            # Add build date as hardware version if available
            if "firmware_build_date" in repeater_stats:
                device_info["hw_version"] = repeater_stats["firmware_build_date"]
            
        self._attr_device_info = DeviceInfo(**device_info)
        
        # Set icon based on stat key
        self._attr_icon = "mdi:radio-tower"
    
    @property
    def is_on(self) -> bool:
        """Return if the repeater is active."""
        if not self.coordinator.data or "repeater_stats" not in self.coordinator.data:
            return False
            
        # Get the repeater stats for this repeater
        repeater_stats = self.coordinator.data.get("repeater_stats", {}).get(self.repeater_name, {})
        if not repeater_stats:
            return False
            
        # Check last updated time if available
        if "last_updated" in repeater_stats:
            # Calculate time since last update
            time_since = time.time() - repeater_stats["last_updated"]
            # If less than 1 hour, consider active
            if time_since < 3600:
                return True
                
        return False
        
    @property
    def state(self) -> str:
        """Return the state of the binary sensor as "fresh" or "stale"."""
        return "fresh" if self.is_on else "stale"
        
    @property
    def available(self) -> bool:
        """Return if the sensor is available."""
        # Check if coordinator is available and we have data for this repeater
        if not super().available or not self.coordinator.data:
            return False
            
        # Check if we have stats for this repeater
        repeater_stats = self.coordinator.data.get("repeater_stats", {})
        return self.repeater_name in repeater_stats
        
    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return additional state attributes."""
        if not self.coordinator.data or "repeater_stats" not in self.coordinator.data:
            return {}
            
        # Get the repeater stats for this repeater
        repeater_stats = self.coordinator.data.get("repeater_stats", {}).get(self.repeater_name, {})
        if not repeater_stats:
            return {}
            
        attributes = {}
        
        # Add key stats as attributes
        for key in ["uptime", "airtime", "nb_sent", "nb_recv", "bat"]:
            if key in repeater_stats:
                attributes[key] = repeater_stats[key]
                
                # Format uptime if available
                if key == "uptime" and isinstance(repeater_stats[key], (int, float)):
                    seconds = repeater_stats[key]
                    days = seconds // 86400
                    hours = (seconds % 86400) // 3600
                    minutes = (seconds % 3600) // 60
                    secs = seconds % 60
                    attributes["uptime_formatted"] = f"{days}d {hours}h {minutes}m {secs}s"
                    
        # Add last updated timestamp if available
        if "last_updated" in repeater_stats:
            attributes["last_updated"] = datetime.fromtimestamp(repeater_stats["last_updated"]).isoformat()
            
        return attributes