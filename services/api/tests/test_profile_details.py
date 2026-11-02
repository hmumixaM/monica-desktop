from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from test_media_integration import db as db  # noqa: PLC0414

from app.repositories import append_field_revision
from app.routers.profile_details import remove_details, save_detail
from app.schemas import DetailRef, DetailRemove, DetailSave

USER = {"id": None}


def current(db, profile_id):
    return dict(db.execute(text("""
        SELECT c.field_key, co.payload_json FROM profile_field_current c
        JOIN profile_field_revisions r ON r.id = c.current_revision_id
        JOIN content_objects co ON co.id = r.content_object_id
        WHERE c.profile_id = :id
    """), {"id": profile_id}).all())


@pytest.fixture
def profile(db):
    profile_id = uuid4()
    db.execute(text("INSERT INTO profiles(id, profile_type, display_name) VALUES (:id, 'person', 'Details')"), {"id": profile_id})
    append_field_revision(db, profile_id=profile_id, field_key="monica.custom_fields", source_type="monica", operation="import",
                          value={"Phone": ["555"], "籍贯": ["杭州"], "LinkedIn": ["https://linkedin.example/in/a"]})
    append_field_revision(db, profile_id=profile_id, field_key="wechat.alias", value="wx", source_type="wechat", operation="import")
    db.commit()
    return profile_id


def test_editing_a_bundled_row_moves_it_out_and_retires_duplicates(db, profile):
    append_field_revision(db, profile_id=profile, field_key="籍贯", value="杭州，后来绍兴", source_type="manual", operation="manual_edit")
    save_detail(profile, DetailSave(field_key="籍贯", value=" 祖籍杭州 ", replaces=[
        DetailRef(field_key="monica.custom_fields", item="籍贯"), DetailRef(field_key="籍贯")]), db, USER)
    fields = current(db, profile)
    assert fields["籍贯"] == "祖籍杭州"
    assert fields["monica.custom_fields"] == {"Phone": ["555"], "LinkedIn": ["https://linkedin.example/in/a"]}


def test_rename_retires_the_old_name(db, profile):
    save_detail(profile, DetailSave(field_key="Hometown", value="Suzhou"), db, USER)
    save_detail(profile, DetailSave(field_key="monica.hometown", value="Suzhou", replaces=[DetailRef(field_key="Hometown")]), db, USER)
    fields = current(db, profile)
    assert fields["Hometown"] is None
    assert fields["monica.hometown"] == "Suzhou"


def test_remove_rows_and_imports_do_not_bring_them_back(db, profile):
    save_detail(profile, DetailSave(field_key="monica.phone", value="555"), db, USER)
    remove_details(profile, DetailRemove(refs=[DetailRef(field_key="monica.phone"),
                                               DetailRef(field_key="monica.custom_fields", item="Phone"),
                                               DetailRef(field_key="monica.custom_fields", item="籍贯"),
                                               DetailRef(field_key="monica.custom_fields", item="LinkedIn")]), db, USER)
    append_field_revision(db, profile_id=profile, field_key="monica.custom_fields", source_type="monica", operation="import",
                          value={"Phone": ["555"]})
    fields = current(db, profile)
    assert fields["monica.phone"] is None
    assert fields["monica.custom_fields"] is None
    operations = db.execute(text("SELECT operation FROM profile_field_revisions WHERE profile_id=:id AND field_key='monica.phone' ORDER BY revision_seq"),
                            {"id": profile}).scalars().all()
    assert operations == ["manual_edit", "manual_delete"]


def test_imported_and_reserved_fields_stay_out_of_reach(db, profile):
    for payload in (DetailSave(field_key="wechat.alias", value="x"), DetailSave(field_key="monica.custom_fields", value="x")):
        with pytest.raises(HTTPException) as error:
            save_detail(profile, payload, db, USER)
        assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        save_detail(profile, DetailSave(field_key="summary", value="x"), db, USER)
    assert error.value.status_code == 422
    for ref in (DetailRef(field_key="wechat.alias"), DetailRef(field_key="monica.custom_fields", item="Missing")):
        with pytest.raises(HTTPException) as error:
            remove_details(profile, DetailRemove(refs=[ref]), db, USER)
        assert error.value.status_code == 404
    db.rollback()
    assert current(db, profile)["wechat.alias"] == "wx"
