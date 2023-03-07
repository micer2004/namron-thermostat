"""Climate on Zigbee Home Automation networks.

For more details on this platform, please refer to the documentation
at https://home-assistant.io/components/zha.climate/
"""


from __future__ import annotations

from datetime import datetime, timedelta
import functools
import logging
import asyncio
import os

from random import randint
from typing import Any

#import homeassistant.zha.climate
from zcl.clusters.hvac import Fan as F, Thermostat as T

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    FAN_AUTO,
    FAN_ON,
    PRESET_AWAY,
    PRESET_BOOST,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_NONE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)


from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    PRECISION_TENTHS,
    Platform,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
import homeassistant.util.dt as dt_util

from .core import discovery
from .core.const import (
    CHANNEL_FAN,
    CHANNEL_THERMOSTAT,
    DATA_ZHA,
    PRESET_COMPLEX,
    PRESET_SCHEDULE,
    PRESET_TEMP_MANUAL,
    SIGNAL_ADD_ENTITIES,
    SIGNAL_ATTR_UPDATED,
)
from .core.registries import ZHA_ENTITIES
from .entity import ZhaEntity

ATTR_SYS_MODE = "system_mode"
ATTR_RUNNING_MODE = "running_mode"
ATTR_SETPT_CHANGE_SRC = "setpoint_change_source"
ATTR_SETPT_CHANGE_AMT = "setpoint_change_amount"
ATTR_OCCUPANCY = "occupancy"
ATTR_PI_COOLING_DEMAND = "pi_cooling_demand"
ATTR_PI_HEATING_DEMAND = "pi_heating_demand"
ATTR_OCCP_COOL_SETPT = "occupied_cooling_setpoint"
ATTR_OCCP_HEAT_SETPT = "occupied_heating_setpoint"
ATTR_UNOCCP_HEAT_SETPT = "unoccupied_heating_setpoint"
ATTR_UNOCCP_COOL_SETPT = "unoccupied_cooling_setpoint"


STRICT_MATCH = functools.partial(ZHA_ENTITIES.strict_match, Platform.CLIMATE)
MULTI_MATCH = functools.partial(ZHA_ENTITIES.multipass_match, Platform.CLIMATE)
RUNNING_MODE = {0x00: HVACMode.OFF, 0x03: HVACMode.COOL, 0x04: HVACMode.HEAT}

SEQ_OF_OPERATION = {
    0x00: [HVACMode.OFF, HVACMode.COOL],  # cooling only
    0x01: [HVACMode.OFF, HVACMode.COOL],  # cooling with reheat
    0x02: [HVACMode.OFF, HVACMode.HEAT],  # heating only
    0x03: [HVACMode.OFF, HVACMode.HEAT],  # heating with reheat
    # cooling and heating 4-pipes
    0x04: [HVACMode.OFF, HVACMode.HEAT_COOL, HVACMode.COOL, HVACMode.HEAT],
    # cooling and heating 4-pipes
    0x05: [HVACMode.OFF, HVACMode.HEAT_COOL, HVACMode.COOL, HVACMode.HEAT],
    0x06: [HVACMode.COOL, HVACMode.HEAT, HVACMode.OFF],  # centralite specific
    0x07: [HVACMode.HEAT_COOL, HVACMode.OFF],  # centralite specific
}

HVAC_MODE_2_SYSTEM = {
    HVACMode.OFF: T.SystemMode.Off,
    HVACMode.HEAT_COOL: T.SystemMode.Auto,
    HVACMode.AUTO: T.SystemMode.Auto,
    HVACMode.COOL: T.SystemMode.Cool,
    HVACMode.HEAT: T.SystemMode.Heat,
    HVACMode.FAN_ONLY: T.SystemMode.Fan_only,
    HVACMode.DRY: T.SystemMode.Dry,
}

SYSTEM_MODE_2_HVAC = {
    T.SystemMode.Off: HVACMode.OFF,
    T.SystemMode.Auto: HVACMode.AUTO,
    T.SystemMode.Cool: HVACMode.COOL,
    T.SystemMode.Heat: HVACMode.HEAT,
    T.SystemMode.Emergency_Heating: HVACMode.HEAT,
    T.SystemMode.Pre_cooling: HVACMode.COOL,  # this is 'precooling'. is it the same?
    T.SystemMode.Fan_only: HVACMode.FAN_ONLY,
    T.SystemMode.Dry: HVACMode.DRY,
    T.SystemMode.Sleep: HVACMode.OFF,
}

ZCL_TEMP = 100

async def async_setup(hass, config):
    hass.states.set("hello_state.world", "Paulus")
    
    # Return boolean to indicate that initialization was successful.
    return True

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Zigbee Home Automation sensor from config entry."""
    entities_to_create = hass.data[DATA_ZHA][Platform.CLIMATE]
    unsub = async_dispatcher_connect(
        hass,
        SIGNAL_ADD_ENTITIES,
        functools.partial(
            discovery.async_add_entities, async_add_entities, entities_to_create
        ),
    )
    config_entry.async_on_unload(unsub)

@STRICT_MATCH(
    channel_names=CHANNEL_THERMOSTAT,
    manufacturers={
        "NAMRON AS",
    },
)
class NamronThermostat(Thermostat):
    """ NAMRON Thermostat implementation """

    def __init__(
        self,
        unique_id,
        zha_device,
        channels, 
        **kwargs
        ):
            """Initialize ZHA Thermostat instance."""
            super().__init__(unique_id, zha_device, channels, **kwargs)
            self._thrm = self.cluster_channels.get(CHANNEL_THERMOSTAT)
            self._preset = PRESET_NONE
            self._presets = []
            self._supported_flags = ClimateEntityFeature.TARGET_TEMPERATURE


    @property
    def current_temperature(self):
        """Return the current temperature."""
        if self._thrm.outdoor_temperature is None:
            return None
        return self._thrm.outdoor_temperature / ZCL_TEMP
    
    @property
    def current_room_temperature(self):
        """Return the current temperature."""
        if self._thrm.local_temperature is None:
            return None
        return self._thrm.local_temperature / ZCL_TEMP
    
    @property
    def current_floor_temperature(self):
        """Return the current temperature."""
        if self._thrm.outdoor_temperature is None:
            return None
        return self._thrm.outdoor_temperature / ZCL_TEMP

    @property
    def extra_state_attributes(self):
        """Return device specific state attributes."""
        data = {}
        if self.hvac_mode:
            mode = SYSTEM_MODE_2_HVAC.get(self._thrm.system_mode, "unknown")
            data[ATTR_SYS_MODE] = f"[{self._thrm.system_mode}]/{mode}"
        if self._thrm.occupancy is not None:
            data[ATTR_OCCUPANCY] = self._thrm.occupancy
        if self._thrm.occupied_cooling_setpoint is not None:
            data[ATTR_OCCP_COOL_SETPT] = self._thrm.occupied_cooling_setpoint
        if self._thrm.occupied_heating_setpoint is not None:
            data[ATTR_OCCP_HEAT_SETPT] = self._thrm.occupied_heating_setpoint
        if self._thrm.pi_heating_demand is not None:
            data[ATTR_PI_HEATING_DEMAND] = self._thrm.pi_heating_demand
        if self._thrm.pi_cooling_demand is not None:
            data[ATTR_PI_COOLING_DEMAND] = self._thrm.pi_cooling_demand

        unoccupied_cooling_setpoint = self._thrm.unoccupied_cooling_setpoint
        if unoccupied_cooling_setpoint is not None:
            data[ATTR_UNOCCP_COOL_SETPT] = unoccupied_cooling_setpoint

        unoccupied_heating_setpoint = self._thrm.unoccupied_heating_setpoint
        if unoccupied_heating_setpoint is not None:
            data[ATTR_UNOCCP_HEAT_SETPT] = unoccupied_heating_setpoint
        return data

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current HVAC action."""
        if (
            self._thrm.pi_heating_demand is None
            and self._thrm.pi_cooling_demand is None
        ):
            return self._rm_rs_action
        return self._pi_demand_action

    @property
    def _rm_rs_action(self) -> HVACAction | None:
        """Return the current HVAC action based on running mode and running state."""

        if (running_state := self._thrm.running_state) is None:
            return None
        if running_state & (
            T.RunningState.Heat_State_On | T.RunningState.Heat_2nd_Stage_On
        ):
            return HVACAction.HEATING
        if running_state & (
            T.RunningState.Cool_State_On | T.RunningState.Cool_2nd_Stage_On
        ):
            return HVACAction.COOLING
        if running_state & (
            T.RunningState.Fan_State_On
            | T.RunningState.Fan_2nd_Stage_On
            | T.RunningState.Fan_3rd_Stage_On
        ):
            return HVACAction.FAN
        if running_state & T.RunningState.Idle:
            return HVACAction.IDLE
        if self.hvac_mode != HVACMode.OFF:
            return HVACAction.IDLE
        return HVACAction.OFF


    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the list of available HVAC operation modes for NAMRON Thermostat"""
        return [HVACMode.AUTO, HVACMode.HEAT, HVACMode.DRY, HVACMode.OFF]


    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Return the list of supported features."""
        features = self._supported_flags
        if HVACMode.AUTO in self.hvac_modes:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        if self._fan is not None:
            self._supported_flags |= ClimateEntityFeature.FAN_MODE
        return features

    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        temp = None
        if self.hvac_mode == HVACMode.COOL:
            if self.preset_mode == PRESET_AWAY:
                temp = self._thrm.unoccupied_cooling_setpoint
            else:
                temp = self._thrm.occupied_cooling_setpoint
        elif self.hvac_mode == HVACMode.HEAT:
            if self.preset_mode == PRESET_AWAY:
                temp = self._thrm.unoccupied_heating_setpoint
            else:
                temp = self._thrm.occupied_heating_setpoint
        elif self.hvac_mode == HVACMode.AUTO:
            if self.preset_mode == PRESET_AWAY:
                temp = self._thrm.unoccupied_heating_setpoint
            else:
                temp = self._thrm.occupied_heating_setpoint
        if temp is None:
            return temp
        return round(temp / ZCL_TEMP, 1)

    @property
    def target_temperature_high(self):
        """Return the upper bound temperature we try to reach."""
        if self.hvac_mode != HVACMode.HEAT_COOL:
            return None
        if self.preset_mode == PRESET_AWAY:
            temp = self._thrm.unoccupied_cooling_setpoint
        else:
            temp = self._thrm.occupied_cooling_setpoint

        if temp is None:
            return temp

        return round(temp / ZCL_TEMP, 1)

    @property
    def target_temperature_low(self):
        """Return the lower bound temperature we try to reach."""
        if self.hvac_mode != HVACMode.HEAT_COOL:
            return None
        if self.preset_mode == PRESET_AWAY:
            temp = self._thrm.unoccupied_heating_setpoint
        else:
            temp = self._thrm.occupied_heating_setpoint

        if temp is None:
            return temp
        return round(temp / ZCL_TEMP, 1)

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature."""
        temps = []
        if HVACMode.HEAT in self.hvac_modes:
            temps.append(self._thrm.max_heat_setpoint_limit)
        if HVACMode.AUTO in self.hvac_modes:
            temps.append(self._thrm.max_heat_setpoint_limit)
        if HVACMode.COOL in self.hvac_modes:
            temps.append(self._thrm.max_cool_setpoint_limit)

        if not temps:
            return self.DEFAULT_MAX_TEMP
        return round(max(temps) / ZCL_TEMP, 1)

    @property
    def min_temp(self) -> float:
        """Return the minimum temperature."""
        temps = []
        if HVACMode.HEAT in self.hvac_modes:
            temps.append(self._thrm.min_heat_setpoint_limit)
        if HVACMode.AUTO in self.hvac_modes:
            temps.append(self._thrm.min_heat_setpoint_limit)
        if HVACMode.COOL in self.hvac_modes:
            temps.append(self._thrm.min_cool_setpoint_limit)

        if not temps:
            return self.DEFAULT_MIN_TEMP
        return round(min(temps) / ZCL_TEMP, 1)

    

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        low_temp = kwargs.get(ATTR_TARGET_TEMP_LOW)
        high_temp = kwargs.get(ATTR_TARGET_TEMP_HIGH)
        temp = kwargs.get(ATTR_TEMPERATURE)
        hvac_mode = kwargs.get(ATTR_HVAC_MODE)

        if hvac_mode is not None:
            await self.async_set_hvac_mode(hvac_mode)

        thrm = self._thrm
        if self.hvac_mode == HVACMode.HEAT_COOL:
            success = True
            if low_temp is not None:
                low_temp = int(low_temp * ZCL_TEMP)
                success = success and await thrm.async_set_heating_setpoint(
                    low_temp, self.preset_mode == PRESET_AWAY
                )
                self.debug("Setting heating %s setpoint: %s", low_temp, success)
            if high_temp is not None:
                high_temp = int(high_temp * ZCL_TEMP)
                success = success and await thrm.async_set_cooling_setpoint(
                    high_temp, self.preset_mode == PRESET_AWAY
                )
                self.debug("Setting cooling %s setpoint: %s", low_temp, success)
        elif temp is not None:
            temp = int(temp * ZCL_TEMP)
            if self.hvac_mode == HVACMode.COOL:
                success = await thrm.async_set_cooling_setpoint(
                    temp, self.preset_mode == PRESET_AWAY
                )
            elif self.hvac_mode == HVACMode.HEAT:
                success = await thrm.async_set_heating_setpoint(
                    temp, self.preset_mode == PRESET_AWAY
                )
            else:
                self.debug("Not setting temperature for '%s' mode", self.hvac_mode)
                return
        else:
            self.debug("incorrect %s setting for '%s' mode", kwargs, self.hvac_mode)
            return

        if success:
            self.async_write_ha_state()
