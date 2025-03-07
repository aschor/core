"""Matter to Home Assistant adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from chip.clusters import Objects as clusters
from matter_server.client.models.device_types import BridgedNode
from matter_server.common.models import EventType, ServerInfoMessage

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, ID_TYPE_DEVICE_ID, ID_TYPE_SERIAL, LOGGER
from .discovery import async_discover_entities
from .helpers import get_device_id

if TYPE_CHECKING:
    from matter_server.client import MatterClient
    from matter_server.client.models.node import MatterEndpoint, MatterNode


def get_clean_name(name: str | None) -> str | None:
    """Strip spaces and null char from the name."""
    if name is None:
        return name
    name = name.replace("\x00", "")
    return name.strip() or None


class MatterAdapter:
    """Connect Matter into Home Assistant."""

    def __init__(
        self,
        hass: HomeAssistant,
        matter_client: MatterClient,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the adapter."""
        self.matter_client = matter_client
        self.hass = hass
        self.config_entry = config_entry
        self.platform_handlers: dict[Platform, AddEntitiesCallback] = {}
        self.discovered_entities: set[str] = set()

    def register_platform_handler(
        self, platform: Platform, add_entities: AddEntitiesCallback
    ) -> None:
        """Register a platform handler."""
        self.platform_handlers[platform] = add_entities

    async def setup_nodes(self) -> None:
        """Set up all existing nodes and subscribe to new nodes."""
        for node in self.matter_client.get_nodes():
            self._setup_node(node)

        def node_added_callback(event: EventType, node: MatterNode) -> None:
            """Handle node added event."""
            self._setup_node(node)

        def node_updated_callback(event: EventType, node: MatterNode) -> None:
            """Handle node updated event."""
            if not node.available:
                return
            # We always run the discovery logic again,
            # because the firmware version could have been changed or features added.
            self._setup_node(node)

        def endpoint_added_callback(event: EventType, data: dict[str, int]) -> None:
            """Handle endpoint added event."""
            node = self.matter_client.get_node(data["node_id"])
            self._setup_endpoint(node.endpoints[data["endpoint_id"]])

        def endpoint_removed_callback(event: EventType, data: dict[str, int]) -> None:
            """Handle endpoint removed event."""
            server_info = cast(ServerInfoMessage, self.matter_client.server_info)
            try:
                node = self.matter_client.get_node(data["node_id"])
            except KeyError:
                return  # race condition
            device_registry = dr.async_get(self.hass)
            endpoint = node.endpoints.get(data["endpoint_id"])
            if not endpoint:
                return  # race condition
            node_device_id = get_device_id(
                server_info,
                node.endpoints[data["endpoint_id"]],
            )
            identifier = (DOMAIN, f"{ID_TYPE_DEVICE_ID}_{node_device_id}")
            if device := device_registry.async_get_device(identifiers={identifier}):
                device_registry.async_remove_device(device.id)

        def node_removed_callback(event: EventType, node_id: int) -> None:
            """Handle node removed event."""
            try:
                node = self.matter_client.get_node(node_id)
            except KeyError:
                return  # race condition
            for endpoint_id in node.endpoints:
                endpoint_removed_callback(
                    EventType.ENDPOINT_REMOVED,
                    {"node_id": node_id, "endpoint_id": endpoint_id},
                )

        self.config_entry.async_on_unload(
            self.matter_client.subscribe_events(
                callback=endpoint_added_callback, event_filter=EventType.ENDPOINT_ADDED
            )
        )
        self.config_entry.async_on_unload(
            self.matter_client.subscribe_events(
                callback=endpoint_removed_callback,
                event_filter=EventType.ENDPOINT_REMOVED,
            )
        )
        self.config_entry.async_on_unload(
            self.matter_client.subscribe_events(
                callback=node_removed_callback, event_filter=EventType.NODE_REMOVED
            )
        )
        self.config_entry.async_on_unload(
            self.matter_client.subscribe_events(
                callback=node_added_callback, event_filter=EventType.NODE_ADDED
            )
        )
        self.config_entry.async_on_unload(
            self.matter_client.subscribe_events(
                callback=node_updated_callback, event_filter=EventType.NODE_UPDATED
            )
        )

    def _setup_node(self, node: MatterNode) -> None:
        """Set up an node."""
        LOGGER.debug("Setting up entities for node %s", node.node_id)
        try:
            for endpoint in node.endpoints.values():
                # Node endpoints are translated into HA devices
                self._setup_endpoint(endpoint)
        except Exception as err:  # noqa: BLE001
            # We don't want to crash the whole setup when a single node fails to setup
            # for whatever reason, so we catch all exceptions here.
            LOGGER.exception(
                "Error setting up node %s: %s",
                node.node_id,
                err,
            )

    def _create_device_registry(
        self,
        endpoint: MatterEndpoint,
    ) -> None:
        """Create a device registry entry for a MatterNode."""
        server_info = cast(ServerInfoMessage, self.matter_client.server_info)

        basic_info = endpoint.device_info
        # use (first) DeviceType of the endpoint as fallback product name
        device_type = next(
            (
                x
                for x in endpoint.device_types
                if x.device_type != BridgedNode.device_type
            ),
            None,
        )
        name = (
            get_clean_name(basic_info.nodeLabel)
            or get_clean_name(basic_info.productLabel)
            or get_clean_name(basic_info.productName)
            or (device_type.__name__ if device_type else None)
        )

        # handle bridged devices
        bridge_device_id = None
        if endpoint.is_bridged_device and endpoint.node.endpoints[0] != endpoint:
            bridge_device_id = get_device_id(
                server_info,
                endpoint.node.endpoints[0],
            )
            bridge_device_id = f"{ID_TYPE_DEVICE_ID}_{bridge_device_id}"

        node_device_id = get_device_id(
            server_info,
            endpoint,
        )
        identifiers = {(DOMAIN, f"{ID_TYPE_DEVICE_ID}_{node_device_id}")}
        serial_number: str | None = None
        # if available, we also add the serialnumber as identifier
        if (
            basic_info_serial_number := basic_info.serialNumber
        ) and "test" not in basic_info_serial_number.lower():
            # prefix identifier with 'serial_' to be able to filter it
            identifiers.add((DOMAIN, f"{ID_TYPE_SERIAL}_{basic_info_serial_number}"))
            serial_number = basic_info_serial_number

        # Model name is the human readable name of the model/product name
        model_name = (
            # productLabel is optional but preferred (e.g. Hue Bloom)
            get_clean_name(basic_info.productLabel)
            # alternative is the productName (e.g. LCT001)
            or get_clean_name(basic_info.productName)
            # if no product name, use the device type name
            or device_type.__name__
            if device_type
            else None
        )
        # Model ID is the non-human readable product ID
        # we prefer the matter product ID so we can look it up in Matter DCL
        if isinstance(basic_info, clusters.BridgedDeviceBasicInformation):
            # On bridged devices, the productID is not available
            model_id = None
        else:
            model_id = str(product_id) if (product_id := basic_info.productID) else None

        dr.async_get(self.hass).async_get_or_create(
            name=name,
            config_entry_id=self.config_entry.entry_id,
            identifiers=identifiers,
            hw_version=basic_info.hardwareVersionString,
            sw_version=basic_info.softwareVersionString,
            manufacturer=basic_info.vendorName or endpoint.node.device_info.vendorName,
            model=model_name,
            model_id=model_id,
            serial_number=serial_number,
            via_device=(DOMAIN, bridge_device_id) if bridge_device_id else None,
        )

    def _setup_endpoint(self, endpoint: MatterEndpoint) -> None:
        """Set up a MatterEndpoint as HA Device."""
        # pre-create device registry entry
        self._create_device_registry(endpoint)
        # run platform discovery from device type instances
        for entity_info in async_discover_entities(endpoint):
            discovery_key = (
                f"{entity_info.platform}_{endpoint.node.node_id}_{endpoint.endpoint_id}_"
                f"{entity_info.primary_attribute.cluster_id}_"
                f"{entity_info.primary_attribute.attribute_id}_"
                f"{entity_info.entity_description.key}"
            )
            if discovery_key in self.discovered_entities:
                continue
            LOGGER.debug(
                "Creating %s entity for %s",
                entity_info.platform,
                entity_info.primary_attribute,
            )
            self.discovered_entities.add(discovery_key)
            new_entity = entity_info.entity_class(
                self.matter_client, endpoint, entity_info
            )
            self.platform_handlers[entity_info.platform]([new_entity])
