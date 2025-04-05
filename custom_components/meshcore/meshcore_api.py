"""API for communicating with MeshCore devices using the meshcore-py library."""
import logging
import asyncio
from sched import Event
from typing import Any, Dict, List, Optional
from asyncio import Lock

from meshcore import MeshCore
from meshcore.events import EventType

from homeassistant.core import HomeAssistant

from .const import (
    CONNECTION_TYPE_USB,
    CONNECTION_TYPE_BLE,
    CONNECTION_TYPE_TCP,
    DEFAULT_BAUDRATE,
    DEFAULT_TCP_PORT,
    NodeType,
    DOMAIN,
)
from .utils import get_node_type_str

_LOGGER = logging.getLogger(__name__)

class MeshCoreAPI:
    """API for interacting with MeshCore devices using the event-driven meshcore-py library."""

    def __init__(
        self,
        hass: HomeAssistant,
        connection_type: str,
        usb_path: Optional[str] = None,
        baudrate: int = DEFAULT_BAUDRATE,
        ble_address: Optional[str] = None,
        tcp_host: Optional[str] = None,
        tcp_port: int = DEFAULT_TCP_PORT,
    ) -> None:
        """Initialize the API."""
        self.hass = hass
        self.connection_type = connection_type
        self.usb_path = usb_path
        self.baudrate = baudrate
        self.ble_address = ble_address
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        
        self._connected = False
        self._connection = None
        self._mesh_core = None
        self._node_info = {}
        self._cached_contacts = {}
        self._cached_messages = []
        
        # Add a lock to prevent concurrent access to the device
        self._device_lock = Lock()
        
    @property
    def mesh_core(self) -> MeshCore:
        """Get the underlying MeshCore instance for direct event subscription."""
        if not self._mesh_core:
            _LOGGER.error("MeshCore instance is not initialized")
            raise RuntimeError("MeshCore instance is not initialized")
        return self._mesh_core
        
    @property
    def connected(self) -> bool:
        """Return whether the device is connected."""
        return self._connected
        
    async def connect(self) -> bool:
        """Connect to the MeshCore device using the appropriate connection type."""
        try:
            # Reset state first
            self._connected = False
            self._mesh_core = None
            
            _LOGGER.info("Connecting to MeshCore device...")
            
            # Create the MeshCore instance using the factory methods based on connection type
            if self.connection_type == CONNECTION_TYPE_USB and self.usb_path:
                _LOGGER.info(f"Using USB connection at {self.usb_path} with baudrate {self.baudrate}")
                self._mesh_core = await MeshCore.create_serial(
                    self.usb_path, 
                    self.baudrate, 
                    debug=False
                )
                
            elif self.connection_type == CONNECTION_TYPE_BLE:
                _LOGGER.info(f"Using BLE connection with address {self.ble_address}")
                self._mesh_core = await MeshCore.create_ble(
                    self.ble_address if self.ble_address else "", 
                    debug=False
                )
                
            elif self.connection_type == CONNECTION_TYPE_TCP and self.tcp_host:
                _LOGGER.info(f"Using TCP connection to {self.tcp_host}:{self.tcp_port}")
                self._mesh_core = await MeshCore.create_tcp(
                    self.tcp_host, 
                    self.tcp_port, 
                    debug=False
                )
                
            else:
                _LOGGER.error("Invalid connection configuration")
                return False
                
            if not self._mesh_core:
                _LOGGER.error("Failed to create MeshCore instance")
                return False
                
            # Load contacts
            _LOGGER.info("Loading contacts...")
            await self._mesh_core.ensure_contacts()
            
            # Fire HA event for successful connection
            if self.hass:
                self.hass.bus.async_fire(f"{DOMAIN}_connected", {
                    "connection_type": self.connection_type
                })
                
            self._connected = True
            _LOGGER.info("Successfully connected to MeshCore device")
            return True
            
        except Exception as ex:
            _LOGGER.error("Error connecting to MeshCore device: %s", ex)
            self._connected = False
            self._mesh_core = None
            return False
    
    async def disconnect(self) -> None:
        """Disconnect from the MeshCore device."""
        try:
            # Trigger device disconnected event
            if self.hass:
                self.hass.bus.async_fire(f"{DOMAIN}_disconnected", {})
                
            # Properly disconnect using the MeshCore instance
            if self._mesh_core:
                try:
                    _LOGGER.info("Cleaning up event subscriptions")
                    if hasattr(self._mesh_core, "dispatcher") and hasattr(self._mesh_core.dispatcher, "subscriptions"):
                        subscription_count = len(self._mesh_core.dispatcher.subscriptions)
                        for subscription in list(self._mesh_core.dispatcher.subscriptions):
                            subscription.unsubscribe()
                        _LOGGER.info(f"Cleared {subscription_count} event subscriptions")
                    # Close the connection
                    _LOGGER.info("Closing connection to MeshCore device")
                    await self._mesh_core.disconnect()
                    _LOGGER.info("Disconnected from MeshCore device")
                except Exception as ex:
                    _LOGGER.error(f"Error during MeshCore disconnect: {ex}")
            
        except Exception as ex:
            _LOGGER.error(f"Error during disconnect: {ex}")
        finally:
            # Always reset these values
            self._connected = False
            self._mesh_core = None
            _LOGGER.info("Disconnection complete")
        return
    
    # async def get_node_info(self) -> Dict[str, Any]:
    #     """Get informatiofn about the node."""
    #     if not self._connected or not self._mesh_core:
    #         _LOGGER.error("Not connected to MeshCore device")
    #         return {}
            
    #     async with self._device_lock:
    #         try:
    #             # Retrieve node info using the MeshCore instance
    #             _LOGGER.info("Requesting full node information")
    #             result = await self._mesh_core.commands.send_appstart()
    #             if result == EventType.ERROR:
    #                 _LOGGER.error("Failed to initialize app session to get node info")
    #                 return {}
                
    #             self._node_info = result.payload.copy()
                
    #             # Try to get device firmware info
    #             try:
    #                 _LOGGER.info("Requesting device firmware and hardware info")
    #                 result = await self._mesh_core.commands.send_device_query()
    #                 if result == EventType.ERROR:
    #                     _LOGGER.error("Failed to get device firmware info")
    #                     return {}
    #                 device_info = result.payload
    #                 _LOGGER.info(f"Device firmware info: version={device_info.get('ver', 'Unknown')}, "
    #                             f"manufacturer={device_info.get('model', 'Unknown')}")
    #                 # Merge device info into node info
    #                 self._node_info.update(device_info)
    #             except Exception as device_ex:
    #                 _LOGGER.warning(f"Could not get device info: {device_ex}")
                
    #             return self._node_info
                
    #         except Exception as ex:
    #             _LOGGER.error("Error getting node info: %s", ex)
    #             return {}
    
    # async def get_battery(self) -> int:
    #     """Get battery level (raw value)."""
    #     if not self._connected or not self._mesh_core:
    #         _LOGGER.error("Not connected to MeshCore device")
    #         return 0
            
    #     async with self._device_lock:
    #         try:
    #             _LOGGER.debug("Getting battery level...")
    #             result = await self._mesh_core.commands.get_bat()
    #             if result is EventType.ERROR:
    #                 _LOGGER.error("Failed to get battery level")
    #                 return 0
                    
    #             return result.payload.get("level", 0)
                
    #         except Exception as ex:
    #             _LOGGER.error("Error getting battery level: %s", ex)
    #             return 0
    
    # async def get_contacts(self) -> Dict[str, Any]:
    #     """Get list of contacts/nodes in the mesh network."""
    #     if not self._connected or not self._mesh_core:
    #         _LOGGER.error("Faile to get contacts: not connected to MeshCore device")
    #         return {}
            
    #     async with self._device_lock:
    #         # Retrieve contacts using the MeshCore instance
    #         _LOGGER.info("Requesting contacts list from device...")
    #         result = await self._mesh_core.commands.get_contacts()
    #         if result is EventType.ERROR:
    #             _LOGGER.error("Failed to get contacts list")

    #         self._cached_contacts = result.payload
    #         contact_count = len(self._cached_contacts)
            
    #         if contact_count > 0:
    #             _LOGGER.info(f"Retrieved {contact_count} contacts")
                
    #             # Log details about each contact for debugging
    #             for _, contact in self._cached_contacts.items():
    #                 _LOGGER.debug(f"Contact: {contact}")
    #                 # map to lat/lon if available
    #                 contact['latitude'] = contact.get('adv_lat')
    #                 contact['longitude'] = contact.get('adv_lon') 

    #         return self._cached_contacts
             
    # async def get_new_messages(self) -> List[Dict[str, Any]]:
    #     """Get new messages from the mesh network.
        
    #     This implementation matches the approach used in mccli.py's sync_msgs command.
    #     It repeatedly calls get_msg() until it returns False, collecting all messages.
    #     """
    #     if not self._connected or not self._mesh_core:
    #         _LOGGER.error("Not connected to MeshCore device")
    #         return []
            
    #     async with self._device_lock:
    #         try:
    #             messages = []
                
    #             _LOGGER.info("===== Syncing messages from device (like mccli.py sync_msgs) =====")
                
    #             # Use a very simple approach that matches the CLI tool's sync_msgs command
    #             res = True
    #             while res:
    #                 res = await self._mesh_core.get_msg()
                    
    #                 if res is False:
    #                     _LOGGER.debug("No more messages (received False)")
    #                     break
                        
    #                 if res:
    #                     # Log message details
    #                     if isinstance(res, dict):
    #                         if "msg" in res:
    #                             text = res.get("msg", "")
    #                             sender = res.get("sender", "Unknown") 
    #                             if hasattr(sender, "hex"):
    #                                 sender = sender.hex()
    #                             timestamp = res.get("sender_timestamp", "Unknown")
                                
    #                             _LOGGER.info(f"Retrieved message: '{text}' from {sender}")
    #                         else:
    #                             _LOGGER.info(f"Retrieved non-text message: {res}")
    #                     else:
    #                         _LOGGER.warning(f"Retrieved non-dict result: {res}")
                        
    #                     # Add to our message list
    #                     messages.append(res)
                        
    #                     # Add to cached messages
    #                     self._cached_messages.append(res)
    #                     # Keep only the latest 50 messages
    #                     if len(self._cached_messages) > 50:
    #                         self._cached_messages = self._cached_messages[-50:]
                    
    #             _LOGGER.info(f"===== Retrieved {len(messages)} messages from device =====")
    #             return messages
                
    #         except Exception as ex:
    #             _LOGGER.error(f"Error getting new messages: {ex}")
    #             return []
        
    # async def wait_for_message(self, timeout: int = 10) -> Optional[Dict[str, Any]]:
    #     """Wait for a new message to arrive."""
    #     if not self._connected or not self._mesh_core:
    #         _LOGGER.error("Not connected to MeshCore device")
    #         return None
            
    #     # We'll use a shorter timeout to avoid blocking the device for too long
    #     actual_timeout = min(timeout, 2)
            
    #     async with self._device_lock:
    #         try:
    #             _LOGGER.debug(f"Waiting for messages with {actual_timeout}s timeout...")
                
    #             # Wait for message notification
    #             got_message = await self._mesh_core.wait_msg(actual_timeout)
    #             if not got_message:
    #                 # Timeout waiting for message
    #                 _LOGGER.debug("No messages received within timeout period")
    #                 return None
                    
    #             _LOGGER.info("Message notification received, fetching message...")
                
    #             # Get the message
    #             msg = await self._mesh_core.get_msg()
    #             if msg:
    #                 _LOGGER.info(f"Message received: {msg}")
                    
    #                 # Add to cached messages
    #                 self._cached_messages.append(msg)
    #                 # Keep only the latest 50 messages
    #                 if len(self._cached_messages) > 50:
    #                     self._cached_messages = self._cached_messages[-50:]
                    
    #                 # We won't check for additional messages here to avoid
    #                 # blocking the device for too long
    #                 return msg
                
    #             _LOGGER.debug("Message notification received but no message data found")
    #             return None
                
    #         except Exception as ex:
    #             _LOGGER.error(f"Error waiting for message: {ex}")
    #             return None
    
    # async def request_status(self) -> Dict[str, Any]:
    #     """Request status from all nodes.
        
    #     Note: Currently disabled to avoid potential device issues.
    #     """
    #     # Skipping status requests due to device issues
    #     _LOGGER.debug("Status requests are disabled to avoid device issues")
    #     return {}  # Return empty status results
        
    # async def login_to_repeater(self, repeater_name: str, password: str) -> bool:
    #     """Login to a specific repeater using its name and password."""
    #     if not self._connected or not self._mesh_core:
    #         _LOGGER.error("Not connected to MeshCore device")
    #         return False
            
    #     async with self._device_lock:
    #         try:
    #             # Find the repeater in contacts
    #             repeater_found = False
    #             repeater_key = None
                
    #             # First ensure we have contacts
    #             if not self._cached_contacts:
    #                 # Get contacts if we don't have them cached
    #                 _LOGGER.info(f"No cached contacts, fetching contacts before login to {repeater_name}")
    #                 self._cached_contacts = await self.get_contacts()
                
    #             # Log the cached contacts for debugging
    #             _LOGGER.info(f"Cached contacts: {list(self._cached_contacts.keys())}")
                
    #             # Look for the repeater by name
    #             for name, contact in self._cached_contacts.items():
    #                 _LOGGER.debug(f"Checking contact: {name}, type: {contact.get('type')}")
    #                 if name == repeater_name:
    #                     repeater_found = True
    #                     # IMPORTANT: Use the full public key as in the CLI code
    #                     repeater_key = bytes.fromhex(contact["public_key"])
    #                     _LOGGER.info(f"Found repeater {repeater_name} with key: {repeater_key.hex()}")
    #                     break
                
    #             if not repeater_found or not repeater_key:
    #                 _LOGGER.error(f"Repeater {repeater_name} not found in contacts")
    #                 return False
                
    #             # Send login command
    #             _LOGGER.info(f"Logging into repeater {repeater_name} with password: {'guest login' if not password else '****'}")
    #             # Handle empty password as guest login
    #             send_result = await self._mesh_core.send_login(repeater_key, password if password else "")
    #             _LOGGER.info(f"Login command result: {send_result}")
                
    #             # Send_login returns True on success, which may be all we need
    #             # Some repeaters respond directly to the login command without sending a notification
    #             if send_result is True:
    #                 _LOGGER.info(f"Login command to repeater {repeater_name} succeeded directly")
    #                 return True
                    
    #             # If direct response wasn't success, try waiting for a notification
    #             _LOGGER.info(f"Waiting for login notification from repeater {repeater_name}")
    #             login_success = await self._mesh_core.wait_login(timeout=5)
                
    #             if login_success:
    #                 _LOGGER.info(f"Successfully logged into repeater {repeater_name}")
    #                 return True
    #             else:
    #                 _LOGGER.error(f"Failed to login to repeater {repeater_name}, timeout or login denied")
    #                 return False
                    
    #         except Exception as ex:
    #             _LOGGER.error(f"Error logging into repeater: {ex}")
    #             _LOGGER.exception("Detailed exception")
    #             return False
    
    # async def get_repeater_stats(self, repeater_name: str) -> Dict[str, Any]:
    #     """Get stats from a repeater after login."""
    #     if not self._connected or not self._mesh_core:
    #         _LOGGER.error("Not connected to MeshCore device")
    #         return {}
            
    #     async with self._device_lock:
    #         try:
    #             # Find the repeater in contacts
    #             repeater_found = False
    #             repeater_key = None
                
    #             # First ensure we have contacts
    #             if not self._cached_contacts:
    #                 # Get contacts if we don't have them cached
    #                 _LOGGER.info(f"No cached contacts, fetching contacts before getting stats for {repeater_name}")
    #                 self._cached_contacts = await self.get_contacts()
                
    #             # Log the cached contacts for debugging
    #             _LOGGER.info(f"Cached contacts for stats: {list(self._cached_contacts.keys())}")
                
    #             # Look for the repeater by name
    #             for name, contact in self._cached_contacts.items():
    #                 if name == repeater_name:
    #                     repeater_found = True
    #                     # IMPORTANT: Use the full public key as in the CLI code
    #                     repeater_key = bytes.fromhex(contact["public_key"])
    #                     _LOGGER.info(f"Found repeater {repeater_name} with key: {repeater_key.hex()} for stats")
    #                     break
                
    #             if not repeater_found or not repeater_key:
    #                 _LOGGER.error(f"Repeater {repeater_name} not found in contacts for stats")
    #                 return {}
                
    #             # Send status request
    #             _LOGGER.info(f"Requesting stats from repeater {repeater_name}")
    #             await self._mesh_core.send_statusreq(repeater_key)
                
    #             # Wait for status response
    #             _LOGGER.info(f"Waiting for stats response from repeater {repeater_name}")
    #             status = await self._mesh_core.wait_status(timeout=5)
                
    #             if status:
    #                 _LOGGER.info(f"Received stats from repeater {repeater_name}: {status}")
    #                 return status
    #             else:
    #                 _LOGGER.warning(f"No stats received from repeater {repeater_name} - timeout waiting for status")
    #                 return {}
                    
    #         except Exception as ex:
    #             _LOGGER.error(f"Error getting repeater stats: {ex}")
    #             _LOGGER.exception("Detailed exception for stats")
    #             return {}
                
    # async def get_repeater_version(self, repeater_name: str) -> Optional[str]:
    #     """Get version information from a repeater using the 'ver' command."""
    #     if not self._connected or not self._mesh_core:
    #         _LOGGER.error("Not connected to MeshCore device")
    #         return None
            
    #     async with self._device_lock:
    #         try:
    #             # Find the repeater in contacts
    #             repeater_found = False
    #             repeater_key = None
                
    #             # First ensure we have contacts
    #             if not self._cached_contacts:
    #                 # Get contacts if we don't have them cached
    #                 _LOGGER.info(f"No cached contacts, fetching contacts before getting version for {repeater_name}")
    #                 self._cached_contacts = await self.get_contacts()
                
    #             # Look for the repeater by name
    #             for name, contact in self._cached_contacts.items():
    #                 if name == repeater_name:
    #                     repeater_found = True
    #                     # IMPORTANT: Use the full public key as in the CLI code
    #                     repeater_key = bytes.fromhex(contact["public_key"])
    #                     _LOGGER.info(f"Found repeater {repeater_name} with key: {repeater_key.hex()} for version info")
    #                     break
                
    #             if not repeater_found or not repeater_key:
    #                 _LOGGER.error(f"Repeater {repeater_name} not found in contacts for version check")
    #                 return None
                
    #             # Send 'ver' command
    #             _LOGGER.info(f"Sending 'ver' command to repeater {repeater_name}")
    #             # Using send_cmd equivalent to "cmd RepeaterName ver" in mccli.py
    #             cmd_result = await self._mesh_core.send_cmd(repeater_key[:6], "ver")
    #             _LOGGER.info(f"Ver command result: {cmd_result}")
                
    #             # Wait for message response (with a reasonable timeout)
    #             _LOGGER.info(f"Waiting for version message from repeater {repeater_name}")
    #             wait_result = await self._mesh_core.wait_msg(timeout=5)
                
    #             if wait_result:
    #                 # Get the message
    #                 message = await self._mesh_core.get_msg()
                    
    #                 # Check if it's a valid version message
    #                 if message and isinstance(message, dict) and "text" in message:
    #                     version_text = message.get("text", "")
    #                     _LOGGER.info(f"Received version from repeater {repeater_name}: {version_text}")
    #                     return version_text
    #                 else:
    #                     _LOGGER.warning(f"Received non-version message from repeater {repeater_name}: {message}")
    #                     return None
    #             else:
    #                 _LOGGER.warning(f"No version message received from repeater {repeater_name} - timeout waiting for response")
    #                 return None
                    
    #         except Exception as ex:
    #             _LOGGER.error(f"Error getting repeater version: {ex}")
    #             _LOGGER.exception("Detailed exception for version check")
    #             return None
    
    # async def send_message(self, node_name: str, message: str) -> tuple[bool, str, str]:
    #     """Send message to a specific node by name.
        
    #     Returns:
    #         tuple: (success, public_key, name)
    #             - success: Whether the message was sent successfully
    #             - public_key: The public key of the node (or empty if failed)
    #             - name: The node name (same as input if successful)
    #     """
    #     if not self._connected or not self._mesh_core:
    #         _LOGGER.error("Not connected to MeshCore device")
    #         return False, "", ""
            
    #     async with self._device_lock:
    #         try:
    #             # First ensure we have contacts
    #             if not self._cached_contacts:
    #                 # We're behind a lock, so avoid calling another locked method
    #                 _LOGGER.error("No cached contacts available - call get_contacts first")
    #                 return False, "", ""
                    
    #             # Check if the node exists
    #             if node_name not in self._cached_contacts:
    #                 _LOGGER.error("Node %s not found in contacts", node_name)
    #                 return False, "", ""
                
    #             # Get the node's public key
    #             contact_pubkey = self._cached_contacts[node_name]["public_key"]
    #             pubkey_prefix = bytes.fromhex(contact_pubkey)[:6]
                
    #             # Send the message using the MeshCore instance
    #             _LOGGER.info(f"Sending message to {node_name} (pubkey: {contact_pubkey[:12]}): {message}")
    #             result = await self._mesh_core.send_msg(
    #                 pubkey_prefix,
    #                 message
    #             )
                
    #             if not result:
    #                 _LOGGER.error(f"Failed to send message to {node_name}")
    #                 return False, "", ""
                    
    #             # Wait for the message ACK with shorter timeout
    #             _LOGGER.debug(f"Waiting for ACK from {node_name}...")
    #             ack_received = await self._mesh_core.wait_ack(3)  # reduced timeout
                
    #             if ack_received:
    #                 _LOGGER.info(f"Message to {node_name} acknowledged")
    #             else:
    #                 _LOGGER.warning(f"No ACK received from {node_name}")
                
    #             # Return success flag, public key, and node name
    #             return ack_received, contact_pubkey, node_name
                
    #         except Exception as ex:
    #             _LOGGER.error(f"Error sending message: {ex}")
    #             return False, "", ""
                
    # async def send_message_by_pubkey(self, pubkey_prefix: str, message: str) -> tuple[bool, str, str]:
    #     """Send message to a node by public key prefix."""
    #     if not self._connected or not self._mesh_core:
    #         _LOGGER.error("Not connected to MeshCore device")
    #         return False, "", ""
            
    #     async with self._device_lock:
    #         try:
    #             if not self._cached_contacts:
    #                 _LOGGER.error("No cached contacts available - call get_contacts first")
    #                 return False, "", ""
                
    #             # Find contact with matching pubkey prefix
    #             found_name = None
    #             full_pubkey = None
                
    #             for name, contact in self._cached_contacts.items():
    #                 if "public_key" in contact and contact["public_key"].startswith(pubkey_prefix):
    #                     found_name = name
    #                     full_pubkey = contact["public_key"]
    #                     break
                
    #             if not full_pubkey:
    #                 _LOGGER.error(f"No contact found with pubkey prefix: {pubkey_prefix}")
    #                 return False, "", ""
                    
    #             pubkey_bytes = bytes.fromhex(full_pubkey)[:6]
                
    #             _LOGGER.info(f"Sending message to {found_name or pubkey_prefix} (pubkey: {full_pubkey[:12]})")
    #             result = await self._mesh_core.send_msg(
    #                 pubkey_bytes,
    #                 message
    #             )
                
    #             if not result:
    #                 _LOGGER.error(f"Failed to send message to pubkey {pubkey_prefix}")
    #                 return False, "", ""
                    
    #             ack_received = await self._mesh_core.wait_ack(3)
                
    #             if ack_received:
    #                 _LOGGER.info(f"Message acknowledged")
    #             else:
    #                 _LOGGER.warning(f"No ACK received")
                
    #             return ack_received, full_pubkey, found_name or ""
                
    #         except Exception as ex:
    #             _LOGGER.error(f"Error sending message by pubkey: {ex}")
    #             return False, "", ""
                
    # async def send_channel_message(self, channel_idx: int, message: str) -> bool:
    #     """Send message to a specific channel by index."""
    #     if not self._connected or not self._mesh_core:
    #         _LOGGER.error("Not connected to MeshCore device")
    #         return False
            
    #     async with self._device_lock:
    #         try:
    #             # Send the message to the channel using the MeshCore instance
    #             _LOGGER.info(f"Sending message to channel {channel_idx}: {message}")
    #             result = await self._mesh_core.send_chan_msg(channel_idx, message)
                
    #             if not result:
    #                 _LOGGER.error(f"Failed to send message to channel {channel_idx}")
    #                 return False
                
    #             # Note: Channel messages don't have ACKs like direct messages
    #             _LOGGER.info(f"Successfully sent message to channel {channel_idx}")
    #             return True
                
    #         except Exception as ex:
    #             _LOGGER.error(f"Error sending channel message: {ex}")
    #             return False
    
    # async def roomserver_ping(self, room_server_name: str) -> List[Dict[str, Any]]:
    #     """Send a keepalive ping to a room server.
        
    #     This method sends a REQ_TYPE_KEEP_ALIVE request packet to the room server
    #     which may trigger the room server to send any queued messages. It assumes 
    #     the caller has already established any necessary authentication.
        
    #     Args:
    #         room_server_name: The name of the room server to ping
            
    #     Returns:
    #         List of message dictionaries retrieved from the room server
    #     """
    #     if not self._connected or not self._mesh_core:
    #         _LOGGER.error("Not connected to MeshCore device")
    #         return []
            
    #     async with self._device_lock:
    #         try:
    #             _LOGGER.info(f"Sending ping to room server: {room_server_name}")
                
    #             # Find the room server's public key
    #             room_server_key = None
    #             for name, contact in self._cached_contacts.items():
    #                 if name == room_server_name:
    #                     room_server_key = bytes.fromhex(contact["public_key"])[:6]
    #                     _LOGGER.info(f"Found room server {room_server_name} with key: {room_server_key.hex()}")
    #                     break
                        
    #             if not room_server_key:
    #                 _LOGGER.error(f"Could not find public key for room server {room_server_name}")
    #                 return []
                
    #             # Send the keep-alive packet
    #             result = await self._mesh_core.send_roomserver_ping(room_server_key)
                
    #             if not result:
    #                 _LOGGER.error(f"Failed to send ping to room server {room_server_name}")
    #                 return []
                
    #             # Wait for and collect messages (responses should come in automatically)
    #             messages = []
                
    #             # Give the room server a moment to respond
    #             await asyncio.sleep(0.5)
                
    #             # Use get_new_messages approach to retrieve any queued messages
    #             res = True
    #             max_attempts = 10  # Limit attempts to avoid endless loop
    #             attempt = 0
                
    #             while res and attempt < max_attempts:
    #                 attempt += 1
    #                 _LOGGER.debug(f"Attempting to retrieve message {attempt}/{max_attempts}")
                    
    #                 res = await self._mesh_core.get_msg()
                    
    #                 if res is False:
    #                     _LOGGER.debug("No more messages (received False)")
    #                     break
                        
    #                 if res:
    #                     # Add context about the room server
    #                     if isinstance(res, dict):
    #                         res["room_server"] = room_server_name
                            
    #                         if "text" in res:
    #                             text = res.get("text", "")
    #                             pubkey_prefix = res.get("pubkey_prefix", "Unknown")
    #                             _LOGGER.info(f"Retrieved message from room server {room_server_name}: '{text}' from {pubkey_prefix}")
    #                         else:
    #                             _LOGGER.info(f"Retrieved non-text message from room server {room_server_name}: {res}")
    #                     else:
    #                         _LOGGER.warning(f"Retrieved non-dict result from room server: {res}")
                        
    #                     # Add to our message list
    #                     messages.append(res)
                        
    #                     # Add to cached messages
    #                     self._cached_messages.append(res)
    #                     # Keep only the latest 50 messages
    #                     if len(self._cached_messages) > 50:
    #                         self._cached_messages = self._cached_messages[-50:]
                
    #             _LOGGER.info(f"Retrieved {len(messages)} messages from room server {room_server_name}")
    #             return messages
                
    #         except Exception as ex:
    #             _LOGGER.error(f"Error pinging room server: {ex}")
    #             _LOGGER.exception("Detailed exception")
    #             return []

    # async def send_cli_command(self, command: str) -> dict:
        """Send arbitrary CLI command to the node.
        
        Note: This feature is currently stubbed out and needs to be restructured completely.
        CLI commands are not supported in the current version.
        
        Args:
            command: The CLI command to send to the node (e.g., "get_bat", "info", etc.)
            
        Returns:
            dict: Result with error indicating feature is not available
        """
        _LOGGER.warning("CLI commands are not currently supported in this version of the integration")
        return {
            "success": False,
            "error": "CLI commands are not supported in the current version",
            "command": command
        }