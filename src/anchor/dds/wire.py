"""Native Cyclone DDS IDL contract. Import only in optional DDS processes."""
from dataclasses import dataclass
from cyclonedds.idl import IdlStruct
from cyclonedds.idl.types import bounded_str, sequence, uint32, uint64, float32


@dataclass
class VibrationBatch(IdlStruct, typename='anchor.dds.VibrationBatchV1'):
    vessel_id: bounded_str[64]
    boot_id: bounded_str[16]
    seq: uint64
    started_at_ms: uint64
    offsets_us: sequence[uint32, 20]
    xyz_g: sequence[float32, 60]

    @classmethod
    def from_json(cls, value):
        return cls(value['vesselId'], value['bootId'], value['seq'], value['startedAtMs'],
                   value['offsetsUs'], [v for axes in value['xyzG'] for v in axes])

    def to_json(self):
        if len(self.xyz_g) != 3 * len(self.offsets_us):
            raise ValueError('DDS axis count mismatch')
        return {'schemaVersion': 1, 'vesselId': self.vessel_id, 'deviceId': 'engine',
                'bootId': self.boot_id, 'seq': self.seq, 'startedAtMs': self.started_at_ms,
                'offsetsUs': list(self.offsets_us),
                'xyzG': [list(self.xyz_g[i:i+3]) for i in range(0, len(self.xyz_g), 3)]}
