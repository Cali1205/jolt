"""The consumption model - the core of jolt.

The physics is calculated per route segment, not a flat value in
kWh/100 km. The reasoning is laid out in detail in konzept-routenplaner.md,
in short:

- Air resistance goes with v². The difference between 110 and 130 km/h is not
  18 %, but 40 %. Without that, the one question you can still influence at
  12 % remaining charge - "will I make it if I drive slower?" - cannot be
  answered.
- At 1.8 t, gradient hits harder than rolling and air resistance combined,
  and regeneration downhill recovers only about 70 %. Over a pass the balance
  is therefore negative, even though you arrive back at the starting
  elevation.
- The auxiliary consumers depend on time, not on distance. In a traffic jam
  the car keeps heating without covering any kilometres - the reason why
  winter trips with traffic jams blow every forecast.

All functions here are pure and without database or network access, so that
tools/check_model.py can run them directly.
"""
import math
from dataclasses import dataclass, field

# Geometry sits below everything and knows nothing - see app/geo.py.
from ..geo import haversine_m, bearing_degree

G = 9.80665
R_AIR = 287.058          # specific gas constant of dry air, J/(kg·K)
P0 = 101325.0             # standard pressure at sea level, Pa


@dataclass
class VehicleValues:
    """The vehicle parameters, detached from the ORM.

    Deliberately a structure of its own: that way the model can be checked
    without a database, and a hypothetical vehicle ("what about a roof box?")
    is a copy with a changed c_w instead of a DB entry.
    """
    mass_kg: float = 1950.0
    c_w: float = 0.28
    frontal_area_m2: float = 2.3
    c_rr: float = 0.010
    eta_drive: float = 0.88
    eta_regen: float = 0.70
    p_aux_w: float = 350.0
    heat_pump: bool = True
    battery_net_kwh: float = 60.0
    reserve_soc: float = 10.0
    correction_factor: float = 1.0
    # A trailer is a quantity of its own and not a bent c_w of the car: it
    # brings mass (already included in `mass_kg`) and its own air-resistance
    # area, c_w times A in m². It is **added** to that of the car; behind the
    # car the trailer runs in its slipstream, which is why it is smaller than
    # its frontal area alone.
    cwa_extra_m2: float = 0.0
    # Hard upper limit in m/s, None = none. The speed slider only scales the
    # routing's assumption and knows no limit: at 130 % the model calculated
    # with 165 km/h, which no production vehicle drives - and a combination
    # with trailer may do 100 in Germany.
    speed_max_ms: float | None = None

    @classmethod
    def from_trip(cls, trip) -> "VehicleValues":
        """The vehicle's values, adapted to *this* trip.

        Two things belong to the trip and not to the car: the payload and
        whatever hangs on the outside. Bringing both together here is the only
        way to ensure that planning, re-planning and recording calculate with
        the same values - before, `from_model(vehicle)` stood in five places,
        and a carrier missing from just one of them yields a plan that no
        longer adds up on the road.
        """
        vals = cls.from_model(trip.vehicle)
        if getattr(trip, "payload_kg", None) is not None:
            vals.mass_kg = trip.vehicle.curb_mass_kg + trip.payload_kg
        factor = getattr(trip, "air_drag_factor", None) or 1.0
        # The surcharge goes on the coefficient and not on the frontal area -
        # mathematically the same, because the two multiply, but the frontal
        # area is a dimension of the car and does not change when wheels hang
        # on the back.
        vals.c_w = vals.c_w * factor

        trailer_kg = getattr(trip, "trailer_kg", None)
        if trailer_kg:
            vals.mass_kg += trailer_kg
            vals.cwa_extra_m2 = getattr(trip, "trailer_cwa_m2", None) or 0.0
        # The trip's limit and the vehicle's: the smaller one applies.
        limits = [g for g in (getattr(trip, "speed_max_kmh", None),
                               vals.speed_max_ms and vals.speed_max_ms * 3.6)
                   if g]
        vals.speed_max_ms = min(limits) / 3.6 if limits else None
        return vals

    @classmethod
    def from_model(cls, vehicle) -> "VehicleValues":
        return cls(mass_kg=vehicle.mass_kg, c_w=vehicle.c_w,
                   frontal_area_m2=vehicle.frontal_area_m2, c_rr=vehicle.c_rr,
                   eta_drive=vehicle.eta_drive, eta_regen=vehicle.eta_regen,
                   p_aux_w=vehicle.p_aux_w, heat_pump=vehicle.heat_pump,
                   # The measured capacity, if there is one - see
                   # `models.Vehicle.capacity_kwh`. That carries through the
                   # whole chain: consumption model, charging plan, forecast.
                   battery_net_kwh=getattr(vehicle, "capacity_kwh",
                                          vehicle.battery_net_kwh),
                   reserve_soc=vehicle.reserve_soc,
                   correction_factor=vehicle.correction_factor,
                   speed_max_ms=(getattr(vehicle, "max_speed_kmh", None) or 0)
                   / 3.6 or None)


@dataclass
class Environment:
    """Weather at a point on the route."""
    temp_c: float = 15.0
    wind_speed_ms: float = 0.0
    # Meteorological: the direction the wind comes from (0 = north).
    wind_direction_degree: float = 0.0


@dataclass
class ProfilePoint:
    km: float
    elevation_m: float
    speed_kmh: float
    kwh_cumulative: float
    soc: float
    minutes_cumulative: float
    # Position carried along so that the stored profile can be evaluated on its
    # own: live tracking and the reserve marker need a coordinate for a
    # kilometre mark without walking the geometry again - and without relying
    # on both lists having the same length.
    lat: float = 0.0
    lon: float = 0.0

    def as_dict(self) -> dict:
        return {"km": self.km, "soc": self.soc, "kwh": self.kwh_cumulative,
                "elevation": self.elevation_m, "speed_kmh": self.speed_kmh,
                "mins": self.minutes_cumulative, "lat": self.lat, "lon": self.lon}


@dataclass
class Profile:
    points: list[ProfilePoint] = field(default_factory=list)
    kwh_total: float = 0.0
    distance_km: float = 0.0
    mins: float = 0.0
    # Driving time without the speed cap. The ratio `mins` to this value is how
    # much the cap lengthens the trip - and thus the factor by which the
    # routing's time can be stretched accordingly.
    minutes_without_cap: float = 0.0
    # Kilometre mark at which the SoC reaches the reserve. None = destination
    # is reached without touching the reserve.
    reserve_at_km: float | None = None
    soc_at_target: float = 0.0
    consumption_kwh_100km: float = 0.0


def air_density(temp_c: float, elevation_m: float) -> float:
    """Air density in kg/m³ from temperature and elevation.

    Both effects are large enough not to be ignored: at -5 °C the air is about
    9 % denser than at 20 °C - air resistance rises by the same share. At
    1500 m elevation it is 15 % thinner.
    """
    # Below sea level the barometric formula keeps applying - cutting off
    # elevations below zero would give away the denser air in the Netherlands.
    # The only limit is against nonsensical values from broken elevation data,
    # which would otherwise produce a root of a negative number.
    elevation = max(-500.0, min(elevation_m, 9000.0))
    pressure = P0 * (1.0 - 2.25577e-5 * elevation) ** 5.25588
    return pressure / (R_AIR * (273.15 + temp_c))


def hvac_power_w(temp_c: float, heat_pump: bool = True) -> float:
    """Power for heating or air conditioning.

    An approximation, not a model of the cabin: the actual demand depends on
    sun, occupants and how often the doors open. The order of magnitude is
    right, though, and it is the second-largest source of error after speed.

    The value is brought closer to the own car via the calibration - this is
    only the starting point.
    """
    if temp_c >= 25.0:
        # Air conditioning: considerably more economical than heating, because
        # the heat pump works in this direction anyway.
        return min(2500.0, 400.0 + (temp_c - 25.0) * 160.0)
    if temp_c >= 18.0:
        return 150.0        # fan only
    heating_demand = (18.0 - temp_c) * 300.0
    if heat_pump:
        heating_demand *= 0.5
    return min(6000.0, 150.0 + heating_demand)


def headwind_ms(bearing: float, environment: Environment) -> float:
    """Headwind component in m/s (negative = tailwind).

    The factor 0.7 scales the wind speed measured at 10 m height down to
    vehicle height - it is noticeably calmer there because of ground friction,
    vegetation and guard rails. Without this correction the model
    systematically overestimates the wind.
    """
    if environment.wind_speed_ms <= 0:
        return 0.0
    difference = math.radians(environment.wind_direction_degree - bearing)
    return 0.7 * environment.wind_speed_ms * math.cos(difference)


def segment_wh(fz: VehicleValues, distance_m: float, elevation_delta_m: float,
               speed_ms: float, environment: Environment, elevation_m: float,
               bearing: float = 0.0) -> tuple[float, float]:
    """Energy demand of a segment in Wh and its duration in seconds.

    The return value can be negative: a long descent feeds back more than the
    auxiliary consumers draw in that time.
    """
    if distance_m <= 0 or speed_ms <= 0:
        return 0.0, 0.0

    duration_s = distance_m / speed_ms
    distance_3d = math.hypot(distance_m, elevation_delta_m)
    sin_theta = elevation_delta_m / distance_3d if distance_3d else 0.0
    cos_theta = distance_m / distance_3d if distance_3d else 1.0

    rho = air_density(environment.temp_c, elevation_m)
    v_air = speed_ms + headwind_ms(bearing, environment)
    # Square with sign: a strong tailwind pushes, it does not brake.
    cwa = fz.c_w * fz.frontal_area_m2 + fz.cwa_extra_m2
    f_air = 0.5 * rho * cwa * v_air * abs(v_air)
    f_roll = fz.c_rr * fz.mass_kg * G * cos_theta
    f_climb = fz.mass_kg * G * sin_theta

    f_total = f_roll + f_air + f_climb
    work_j = f_total * distance_3d

    if work_j >= 0:
        rad_j = work_j / fz.eta_drive
    else:
        # Downhill: only the regenerated share comes back. Whatever went
        # beyond that is burned off by the friction brake - hence no full
        # recovery.
        rad_j = work_j * fz.eta_regen

    aux_j = (fz.p_aux_w + hvac_power_w(environment.temp_c, fz.heat_pump)) * duration_s
    wh = (rad_j + aux_j) / 3600.0
    return wh * fz.correction_factor, duration_s


def compute_profile(fz: VehicleValues, points: list, speed_ms: list,
                   start_soc: float, environment_for=None,
                   speed_factor: float = 1.0,
                   distance_factor: float = 1.0,
                   speed_cap: bool = True) -> Profile:
    """The energy profile over the entire route.

    `points`       : [[lon, lat, elevation], ...] from the routing
    `speed_ms`     : speed per sub-segment, length len(points) - 1
    `environment_for`: function (lat, lon) -> environment. None = default
                    weather.
    `speed_factor`: 1.1 means "ten percent faster than the routing assumes".
                    Exactly the control with which you can save a charging
                    stop on the road - it acts disproportionately via v².
    `distance_factor`: Stretches every sub-segment. Needed for **recordings**
                    whose support points are far apart: between two GPS
                    reports thirty seconds apart there are four hundred
                    metres at country-road speed, and the straight line in
                    between cuts off every curve. The vehicle's odometer knows
                    better; `live/recording.py` derives the factor from it.
                    It acts on rolling and air resistance as well as on the
                    gradient - a longer distance with the same elevation
                    difference is a flatter gradient, and that is exactly how
                    it was driven.
    `speed_cap`   : Applies `fz.speed_max_ms`. Off for **recordings**: what
                    was driven is not retroactively trimmed to the limit -
                    otherwise the measured consumption would no longer match
                    the measured distance.
    """
    default_environment = Environment()
    fetch_environment = environment_for or (lambda lat, lon: default_environment)

    capacity_wh = fz.battery_net_kwh * 1000.0
    soc = start_soc
    kwh_cum = 0.0
    meter_cum = 0.0
    seconds_cum = 0.0
    seconds_without_cap = 0.0
    reserve_at_km = None

    result = Profile()
    if not points:
        return result

    hoehe0 = points[0][2] if len(points[0]) > 2 else 0.0
    result.points.append(ProfilePoint(0.0, hoehe0, 0.0, 0.0, soc, 0.0,
                                       lat=points[0][1], lon=points[0][0]))

    for i in range(len(points) - 1):
        lon1, lat1 = points[i][0], points[i][1]
        lon2, lat2 = points[i + 1][0], points[i + 1][1]
        h1 = points[i][2] if len(points[i]) > 2 else 0.0
        h2 = points[i + 1][2] if len(points[i + 1]) > 2 else 0.0

        distance = haversine_m(lat1, lon1, lat2, lon2) * distance_factor
        if distance <= 0:
            continue

        v = (speed_ms[i] if i < len(speed_ms) else 25.0) * speed_factor
        v = max(2.0, v)
        v_free = v
        if speed_cap and fz.speed_max_ms:
            v = max(2.0, min(v, fz.speed_max_ms))
        environment = fetch_environment(lat1, lon1)
        bearing = bearing_degree(lat1, lon1, lat2, lon2)

        wh, duration = segment_wh(fz, distance, h2 - h1, v, environment,
                               (h1 + h2) / 2.0, bearing)

        kwh_cum += wh / 1000.0
        meter_cum += distance
        seconds_cum += duration
        seconds_without_cap += distance / v_free
        soc = start_soc - (kwh_cum * 1000.0 / capacity_wh) * 100.0

        if reserve_at_km is None and soc <= fz.reserve_soc:
            reserve_at_km = meter_cum / 1000.0

        result.points.append(ProfilePoint(
            km=round(meter_cum / 1000.0, 3), elevation_m=h2,
            speed_kmh=round(v * 3.6, 1), kwh_cumulative=round(kwh_cum, 3),
            soc=round(soc, 2), minutes_cumulative=round(seconds_cum / 60.0, 1),
            lat=lat2, lon=lon2))

    result.kwh_total = round(kwh_cum, 3)
    result.distance_km = round(meter_cum / 1000.0, 2)
    result.mins = round(seconds_cum / 60.0, 1)
    result.minutes_without_cap = round(seconds_without_cap / 60.0, 1)
    result.reserve_at_km = round(reserve_at_km, 2) if reserve_at_km else None
    result.soc_at_target = round(soc, 2)
    if meter_cum > 0:
        result.consumption_kwh_100km = round(kwh_cum / (meter_cum / 1000.0) * 100.0, 2)
    return result


def thin_out(points: list, speed_ms: list, min_distance_m: float = 250.0):
    """Condense support points without losing the elevation profile.

    On a long route the routing delivers five-digit numbers of points - for
    map and forecast that is computing time without insight. Points are only
    merged as long as the elevation barely changes: a point that deviates by
    more than three metres from the last one always stays. Otherwise exactly
    what drives consumption the most would disappear.
    """
    if len(points) < 3:
        return points, speed_ms

    new_points = [points[0]]
    new_speed: list[float] = []
    distance_since = 0.0
    speed_sum = 0.0
    weight = 0.0
    latest_elevation = points[0][2] if len(points[0]) > 2 else 0.0

    for i in range(len(points) - 1):
        lon1, lat1 = points[i][0], points[i][1]
        lon2, lat2 = points[i + 1][0], points[i + 1][1]
        d = haversine_m(lat1, lon1, lat2, lon2)
        v = speed_ms[i] if i < len(speed_ms) else 25.0
        distance_since += d
        speed_sum += v * d
        weight += d
        elevation = points[i + 1][2] if len(points[i + 1]) > 2 else 0.0

        last = (i + 1) == len(points) - 1
        if (distance_since >= min_distance_m or abs(elevation - latest_elevation) > 3.0
                or last):
            new_points.append(points[i + 1])
            new_speed.append(speed_sum / weight if weight else v)
            distance_since = 0.0
            speed_sum = 0.0
            weight = 0.0
            latest_elevation = elevation

    return new_points, new_speed
