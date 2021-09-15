
import logging
import asyncio
import voluptuous as vol
from datetime import timedelta
import math
import homeassistant.util.dt as dt_util
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.event import async_track_state_change

from homeassistant.helpers.restore_state import (
    RestoreEntity,
)

from homeassistant.components.switch import (
    ENTITY_ID_FORMAT,
    PLATFORM_SCHEMA,
    SwitchEntity,
)

from .const import (
    DOMAIN,
    ATTR_START,
    ATTR_RUN_FREQ,
    ATTR_RUN_DAYS,
    ATTR_IRRIGATION_ON,
    ATTR_RAIN_SENSOR,
    ATTR_IGNORE_RAIN_BOOL,
    CONST_SWITCH,
    ATTR_IGNORE_RAIN_SENSOR,
    ATTR_ZONES,
    ATTR_ZONE,
    ATTR_WATER,
    ATTR_WATER_ADJUST,
    ATTR_WAIT,
    ATTR_REPEAT,
    ATTR_REMAINING,
    DFLT_ICON_WAIT,
    DFLT_ICON_RAIN,
    DFLT_ICON,
    ATTR_LAST_RAN,
)

from homeassistant.const import (
    EVENT_HOMEASSISTANT_START,
    ATTR_ENTITY_ID,
    CONF_SWITCHES,
    CONF_UNIQUE_ID,
    CONF_NAME,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    ATTR_ICON,
    MATCH_ALL,
)

SWITCH_SCHEMA = vol.All(
    cv.deprecated(ATTR_ENTITY_ID),
    vol.Schema(
        {
        vol.Optional(CONF_NAME): cv.string,
        vol.Required(ATTR_START): cv.entity_domain('input_datetime'),
        vol.Exclusive(ATTR_RUN_FREQ,"FRQP"): cv.entity_domain('input_select'),
        vol.Exclusive(ATTR_RUN_DAYS,"FRQP"): cv.entity_domain('input_select'),
        vol.Optional(ATTR_IRRIGATION_ON): cv.entity_domain('input_boolean'),
        vol.Optional(ATTR_ICON,default=DFLT_ICON): cv.icon,
        vol.Required(ATTR_ZONES): [{
            vol.Required(ATTR_ZONE): cv.entity_domain(CONST_SWITCH),
            vol.Required(CONF_NAME): cv.string,
            vol.Optional(ATTR_RAIN_SENSOR): cv.entity_domain('binary_sensor'),
            vol.Optional(ATTR_IGNORE_RAIN_SENSOR): cv.entity_domain('input_boolean'),
            vol.Required(ATTR_WATER): cv.entity_domain('input_number'),
            vol.Optional(ATTR_WATER_ADJUST): cv.entity_domain('input_number'),
            vol.Optional(ATTR_WAIT): cv.entity_domain('input_number'),
            vol.Optional(ATTR_REPEAT): cv.entity_domain('input_number'),
            vol.Optional(ATTR_ICON,default=DFLT_ICON): cv.icon,
        }],
        vol.Optional(CONF_UNIQUE_ID): cv.string,
        }
    ),
)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {vol.Required(CONF_SWITCHES): cv.schema_with_slug_keys(SWITCH_SCHEMA)}
)

_LOGGER = logging.getLogger(__name__)


async def _async_create_entities(hass, config):
    """Create the Template switches."""
    switches = []

    for device, device_config in config[CONF_SWITCHES].items():
        friendly_name           = device_config.get(CONF_NAME, device)
        start_time              = device_config.get(ATTR_START)
        run_freq                = device_config.get(ATTR_RUN_FREQ)
        run_days                = device_config.get(ATTR_RUN_DAYS)
        irrigation_on           = device_config.get(ATTR_IRRIGATION_ON)
        icon                    = device_config.get(ATTR_ICON)
        zones                   = device_config.get(ATTR_ZONES)
        unique_id               = device_config.get(CONF_UNIQUE_ID)

        switches.append(
            IrrigationProgram(
                hass,
                device,
                friendly_name,
                start_time,
                run_freq,
                run_days,
                irrigation_on,
                icon,
                DFLT_ICON_WAIT,
                DFLT_ICON_RAIN,
                zones,
                unique_id,
            )
        )

    return switches


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the irrigation switches."""
    async_add_entities(await _async_create_entities(hass, config))


class IrrigationProgram(SwitchEntity, RestoreEntity):
    """Representation of an Irrigation program."""
    def __init__(
        self,
        hass,
        device_id,
        friendly_name,
        start_time,
        run_freq,
        run_days,
        irrigation_on,
        icon,
        wait_icon,
        rain_icon,
        zones,
        unique_id,
    ):

        self.entity_id = async_generate_entity_id(
            ENTITY_ID_FORMAT, device_id, hass=hass
        )

        """Initialize a Irrigation program."""
        self._name               = str(friendly_name).title()
        self._program_name       = str(friendly_name).title()
        self._start_time         = start_time
        self._run_freq           = run_freq
        self._run_days           = run_days
        self._irrigation_on      = irrigation_on
        self._icon               = icon
        self._wait_icon          = wait_icon
        self._rain_icon          = rain_icon
        self._zones              = zones
        self._state_attributes   = None
        self._state              = False
        self._unique_id          = unique_id
        self._stop               = False
        self._device_id          = device_id
        self._running            = False
        self._last_run           = None
        self._triggered_manually = True
        self._template           = None

        """ Validate and Build a template from the attributes provided """

        _LOGGER.debug('Start Time %s: %s',self._start_time, hass.states.get(self._start_time))
        template = "states('sensor.time')" + " + ':00' == states('" + self._start_time + "') "

        if self._irrigation_on is not None:
            _LOGGER.debug('Irrigation on %s: %s',self._irrigation_on, hass.states.get(self._irrigation_on))
            template = template + " and is_state('" + self._irrigation_on + "', 'on') "

        if self._run_days is not None:
            _LOGGER.debug('Run Days %s: %s',self._run_days, hass.states.get(self._run_days))
            template = template + " and now().strftime('%a') in states('" + self._run_days + "')"

        if self._run_freq is not None:
            _LOGGER.debug('Run Frequency %s: %s',self._run_freq, hass.states.get(self._run_freq))
            template = template + \
                    " and states('" + run_freq + "')|int" + \
                    " <= ((as_timestamp(now()) " + \
                    "- as_timestamp(states." + self.entity_id + \
                    ".attributes.last_ran) | int) /86400) | int(0) "

        template = "{{ " + template + " }}"

        _LOGGER.debug('-------------------- on start: %s ----------------------------',self._name)
        _LOGGER.debug('Template: %s', template)

        template       = cv.template(template)
        template.hass  = hass
        self._template = template


    @callback
    def _update_state(self, result):
        super()._update_state(result)


    async def async_added_to_hass(self):

        state = await self.async_get_last_state()
        try:
            self._last_run = state.attributes.get(ATTR_LAST_RAN)
        except:
            """ default to 10 days ago for new programs """
            now       = dt_util.utcnow() - timedelta(days=10)
            time_date = dt_util.start_of_local_day(dt_util.as_local(now))
            if self._last_run is None:
                self._last_run = dt_util.as_local(time_date).date().isoformat()

        ATTRS = {}
        ATTRS [ATTR_LAST_RAN]  = self._last_run
        ATTRS [ATTR_REMAINING] = 0
        setattr(self, '_state_attributes', ATTRS)
        
        """ house keeping to help ensure solenoids are in a safe state """
        self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_START, self.async_turn_off())

        @callback
        async def template_check(entity, old_state, new_state):
            self.async_schedule_update_ha_state(True)

        @callback
        def template_sensor_startup(event):
            """Triggered when HASS has started"""

            """ Validate the referenced objects now that HASS has started"""
            if  self.hass.states.async_available('sensor.time'):
                _LOGGER.error('%s :check your configuration, ' + \
                                'if the entity has not been defined the irriagtion program will not behave as expected' \
                                ,'sensor.time')

            if  self.hass.states.async_available(self._start_time):
                _LOGGER.warning('%s not found, check your configuration, ' + \
                                'this may be due to the slow start of HomeAssistant, ' + \
                                'if the entity has not been defined the irriagtion program will behave as expected' \
                                ,self._start_time)

            if self._irrigation_on is not None:
                if  self.hass.states.async_available(self._irrigation_on):
                    _LOGGER.warning('%s not found, check your configuration',self._irrigation_on)

            if self._run_days is not None:
                if  self.hass.states.async_available(self._run_days):
                    _LOGGER.warning('%s not found, check your configuration',self._run_days)

            if self._run_freq is not None:
                if  self.hass.states.async_available(self._run_freq):
                    _LOGGER.warning('%s not found, check your configuration',self._run_freq)

            if  self.hass.states.async_available('sensor.time'):
                _LOGGER.error('%s :check your configuration, ' + \
                                'if the entity has not been defined the irriagtion program will not behave as expected' \
                                ,'sensor.time')                

            """Update template on startup """
            async_track_state_change(
                self.hass, 'sensor.time', template_check)


        self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_START, template_sensor_startup)

        await super().async_added_to_hass()


    @property
    def name(self):
        """Return the name of the variable."""
        return self._name

    @property
    def unique_id(self):
        """Return the unique id of this switch."""
        return self._unique_id

    @property
    def is_on(self):
        """Return true if light is on."""
        return self._state

    @property
    def should_poll(self):
        """If entity should be polled."""
        return False

    @property
    def icon(self):
        """Return the icon to be used for this entity."""
        return self._icon

    @property
    def state_attributes(self):
        """Return the state attributes.
        Implemented by component base class.
        """
        return self._state_attributes

    async def async_update(self):

        """Update the state from the template."""
        if self._running == False:
            if self._template.async_render():
                self._triggered_manually = False
                loop = asyncio.get_event_loop()
                loop.create_task(self.async_turn_on())

        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        
        """ Initialise for stop programs service call """
        p_icon        = self._icon
        p_name        = self._name
        self._running = True
        self._stop    = False
        self._state   = True
        self.async_schedule_update_ha_state()
        step = 1

        """ stop all programs but this one """
        DATA = {'ignore': self._device_id}
        await self.hass.services.async_call(DOMAIN,
                                            'stop_programs',
                                            DATA)
        await asyncio.sleep(1)

        _LOGGER.debug('-------------------- on execution: %s ----------------------------',self._name)
        _LOGGER.debug('Template: %s', self._template)
        if self._start_time is not None:
            _LOGGER.debug('Start Time %s: %s',self._start_time, self.hass.states.get(self._start_time))
        if self._irrigation_on is not None:
            _LOGGER.debug('Irrigation on %s: %s',self._irrigation_on, self.hass.states.get(self._irrigation_on))
        if self._run_days is not None:
            _LOGGER.debug('Run Days %s: %s',self._run_days, self.hass.states.get(self._run_days))
        if self._run_freq is not None:
            _LOGGER.debug('Run Frequency %s: %s',self._run_freq, self.hass.states.get(self._run_freq))

        """ Iterate through all the defined zones """
        for zone in self._zones:
            z_rain_sen_v  = zone.get(ATTR_RAIN_SENSOR)
            z_ignore_v    = zone.get(ATTR_IGNORE_RAIN_SENSOR)
            z_zone        = zone.get(ATTR_ZONE)
            z_water_v     = zone.get(ATTR_WATER)
            z_water_adj_v = zone.get(ATTR_WATER_ADJUST)
            z_wait_v      = zone.get(ATTR_WAIT)
            z_repeat_v    = zone.get(ATTR_REPEAT)
            z_icon        = zone.get(ATTR_ICON)
            z_name        = zone.get(CONF_NAME)
            z_ignore_bool = False

            if  z_ignore_v is not None and self.hass.states.async_available(z_ignore_v):
                _LOGGER.error('%s not found',z_ignore_v)
                continue
            if  z_water_v is not None and self.hass.states.async_available(z_water_v):
                _LOGGER.error('%s not found',z_water_v)
                continue
            if  z_water_adj_v is not None and self.hass.states.async_available(z_water_adj_v):
                _LOGGER.error('%s not found',z_water_adj_v)
                continue
            if  z_rain_sen_v is not None and self.hass.states.async_available(z_rain_sen_v):
                _LOGGER.error('%s not found',z_rain_sen_v)
                continue
            if  z_wait_v is not None and self.hass.states.async_available(z_wait_v):
                _LOGGER.error('%s not found',z_wait_v)
            if  z_repeat_v is not None and self.hass.states.async_available(z_repeat_v):
                _LOGGER.error('%s not found',z_repeat_v)

            _LOGGER.debug('------------ on execution zone: %s--------', z_zone)
            raining = False

            if self._triggered_manually == True:
                _LOGGER.debug('------------Irrigation Manually triggered, rain sensor not evaluated--------')
            else:
                """ assess the rain sensor """
                if z_rain_sen_v is not None:
                    _LOGGER.debug('rain sensor: %s',self.hass.states.get(z_rain_sen_v))
                    if  self.hass.states.get(z_rain_sen_v) == None:
                        _LOGGER.warning('rain sensor: %s not found, check your configuration',z_rain_sen_v)
                    else:
                        raining = self.hass.states.is_state(z_rain_sen_v,'on')
                        _LOGGER.debug('raining:%s',raining)
                """ assess the ignore rain sensor """
                if  z_ignore_v is not None:
                    _LOGGER.debug('Ignore rain sensor: %s',self.hass.states.get(z_ignore_v))
                    if  self.hass.states.get(z_ignore_v) == None:
                        _LOGGER.warning('Ignore rain sensor: %s not found, check your configuration',z_ignore_v)
                    else:
                         z_ignore_bool = self.hass.states.is_state(z_ignore_v,'on')
                """ process rain sensor """
                if not z_ignore_bool: #ignore rain sensor
                    _LOGGER.debug('Do not ignore the rain sensor')
                    if raining:
                        _LOGGER.debug('raining do not run, continue to next zone')
                        ''' set the icon to Raining - for a few seconds '''
                        self._icon = self._rain_icon
                        self._name = self._program_name + "-" + z_name
                        self.async_schedule_update_ha_state()
                        await asyncio.sleep(5)
                        self._icon = p_icon
                        self._name = self._program_name
                        self.async_schedule_update_ha_state()
                        await asyncio.sleep(1)
                        continue

            if self._stop == True:
                break

            """ factor to adjust watering time """
            z_water_adj = 1
            if z_water_adj_v is not None:
                z_water_adj = float(self.hass.states.get(z_water_adj_v).state)
                _LOGGER.debug('watering adjustment factor is %s', z_water_adj)

            z_water = math.ceil(int(float(self.hass.states.get(z_water_v).state)) * float(z_water_adj))
            if z_water == 0:
                _LOGGER.debug('watering time has been adjusted to 0 do not run zone %s',z_zone)
                continue
                
            z_wait = 0
            if z_wait_v is not None:
                z_wait = int(float(self.hass.states.get(z_wait_v).state))

            z_repeat = 1
            if z_repeat_v is not None:
                z_repeat = int(float(self.hass.states.get(z_repeat_v).state))
                if z_repeat == 0:
                    z_repeat = 1

            _LOGGER.debug('Start water:%s, water_adj:%s wait:%s, repeat:%s', z_water, z_water_adj, z_wait, z_repeat)

            self._runtime = (((z_water + z_wait) * z_repeat) - z_wait) * 60
            """Set time remaining attribute """
            ATTRS = {}
            ATTRS [ATTR_LAST_RAN] = self._last_run
            ATTRS [ATTR_REMAINING] = self._runtime
            setattr(self, '_state_attributes', ATTRS)
 
            """ run the watering cycle, water/wait/repeat """
            DATA = {ATTR_ENTITY_ID: z_zone}
            _LOGGER.debug('switch data:%s',DATA)
            for i in range(z_repeat, 0, -1):
                _LOGGER.debug('run switch repeat:%s',i)
                if self._stop == True:
                    break
                self._name = self._program_name + "-" + z_name
                if self.hass.states.is_state(z_zone,'off'):
                    await self.hass.services.async_call(CONST_SWITCH,
                                                        SERVICE_TURN_ON,
                                                        DATA)

                self._icon = z_icon
                self.async_schedule_update_ha_state()

                water = z_water * 60
                for w in range(0,water, step):
                    self._runtime = self._runtime - step
                    ATTRS = {}
                    ATTRS [ATTR_LAST_RAN] = self._last_run
                    ATTRS [ATTR_REMAINING] = self._runtime
                    setattr(self, '_state_attributes', ATTRS)
                    self.async_schedule_update_ha_state()
                    await asyncio.sleep(step)
                    if self._stop == True:
                        break

                """ turn the switch entity off """
                if z_wait > 0 and i > 1 and not self._stop:
                    """ Eco mode is enabled """
                    self._icon = self._wait_icon
                    self.async_schedule_update_ha_state()
                    if self.hass.states.is_state(z_zone,'on'):
                        await self.hass.services.async_call(CONST_SWITCH,
                                                            SERVICE_TURN_OFF,
                                                            DATA)

                    wait = z_wait * 60
                    for w in range(0,wait, step):
                        self._runtime = self._runtime - step
                        ATTRS = {}
                        ATTRS [ATTR_LAST_RAN] = self._last_run
                        ATTRS [ATTR_REMAINING] = self._runtime
                        setattr(self, '_state_attributes', ATTRS)
                        self.async_schedule_update_ha_state()
                        await asyncio.sleep(step)
                        if self._stop == True:
                            break

                if i <= 1 or self._stop:
                    """ last/only cycle """
                    if self.hass.states.is_state(z_zone,'on'):
                        await self.hass.services.async_call(CONST_SWITCH,
                                                            SERVICE_TURN_OFF,
                                                            DATA)

        """ end of for zone loop """

        """Update last run date attribute """
        if not self._triggered_manually:
            now            = dt_util.utcnow()
            time_date      = dt_util.start_of_local_day(dt_util.as_local(now))
            self._last_run = dt_util.as_local(time_date).date().isoformat()

        ATTRS = {}
        ATTRS [ATTR_LAST_RAN] = self._last_run
        ATTRS [ATTR_REMAINING] = self._runtime
        setattr(self, '_state_attributes', ATTRS)

        self._state                 = False
        self._running               = False
        self._stop                  = False
        self._triggered_manually    = True
        self._icon                  = p_icon
        self._name                  = self._program_name

        self.async_write_ha_state()
        _LOGGER.debug('program run complete')


    async def async_turn_off(self, **kwargs):

        self._stop = True 
 
        for zone in self._zones:
            z_zone = zone.get(ATTR_ZONE)
            DATA = {ATTR_ENTITY_ID: z_zone}
            _LOGGER.debug('Zone switch %s: %s',z_zone, self.hass.states.get(z_zone))
            if self.hass.states.is_state(z_zone,'on'):
                await self.hass.services.async_call(CONST_SWITCH,
                                                    SERVICE_TURN_OFF,
                                                    DATA)

        self._state = False
        self.async_schedule_update_ha_state()
