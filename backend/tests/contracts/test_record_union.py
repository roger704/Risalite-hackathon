import typing

from app.api.main import RecordsResponse
from app.domain.models import RECORD_TYPES


def test_records_response_accepts_every_registered_record_type():
    union = RecordsResponse.model_fields["records"].annotation.__args__[0]
    missing = set(RECORD_TYPES.values()) - set(typing.get_args(union))
    assert not missing, sorted(cls.__name__ for cls in missing)
