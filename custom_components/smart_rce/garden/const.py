"""Garden constants — mammotion (Luba) entity ids we read / drive.

Single robot for now; entity ids are constants (config_flow ownership deferred —
YAGNI). The reader/actuator take them as params so they stay testable.
"""

LUBA_NON_WORK_SENSOR = "sensor.garden_luba_mn9xcnvu_non_work_hours"
LUBA_LAWN_MOWER = "lawn_mower.luba_mn9xcnvu"
LUBA_BATTERY_SENSOR = "sensor.luba_mn9xcnvu_battery"
LUBA_PROGRESS_SENSOR = "sensor.luba_mn9xcnvu_progress"
LUBA_CHARGING_SENSOR = "binary_sensor.luba_mn9xcnvu_charging"
