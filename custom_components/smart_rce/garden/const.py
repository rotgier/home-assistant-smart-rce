"""Garden constants — mammotion (Luba) entity ids we read / drive.

Single robot for now; entity ids are constants (config_flow ownership deferred —
YAGNI). The reader/actuator take them as params so they stay testable.
"""

LUBA_NON_WORK_SENSOR = "sensor.garden_luba_mn9xcnvu_non_work_hours"
LUBA_LAWN_MOWER = "lawn_mower.luba_mn9xcnvu"
