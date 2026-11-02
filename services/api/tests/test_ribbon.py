from sqlalchemy import text
from test_ui_endpoints import account, person

from app.ribbon import profile_ribbon


def test_ribbon_joins_messages_calls_and_moments_by_month(crm_db):
    friend, other = person(crm_db, "Friend"), person(crm_db, "Other")
    wechat = account(crm_db, "wxid_ribbon")
    crm_db.execute(text("INSERT INTO source_account_links(account_id,profile_id,evidence) VALUES(:a,:p,'{}'::jsonb)"), {"a": wechat, "p": friend})
    crm_db.execute(text("""INSERT INTO source_chat_months(account_id,month,sent,received,total,snapshot_at,timezone)
        VALUES(:a,'2025-01-01',3,4,7,now(),'Asia/Shanghai'),(:a,'2025-03-01',1,0,1,now(),'Asia/Shanghai')"""), {"a": wechat})
    # 2025-04-30 20:00 UTC is already May in Shanghai.
    crm_db.execute(text("""INSERT INTO source_contact_events(account_id,event_kind,external_id,occurred_at,snapshot_at,content)
        VALUES(:a,'call','c1','2025-04-30 20:00+00',now(),'{}'),(:a,'money','m1','2025-01-05 00:00+00',now(),'{}')"""), {"a": wechat})
    post = crm_db.execute(text("""INSERT INTO social_posts(provider,external_id,profile_id,occurred_at,current_content_hash)
        VALUES('wechat','ribbon-post',:p,'2025-03-02',:h) RETURNING id"""), {"p": other, "h": "h" * 64}).scalar_one()
    crm_db.execute(text("""INSERT INTO social_interactions(provider,post_id,interaction_type,external_id,author_profile_id,occurred_at,current_content_hash)
        VALUES('wechat',:post,'like','l1',:p,'2025-03-02 08:00+00',:h),('wechat',:post,'comment','c1',:p,'2025-03-03 08:00+00',:h),
              ('wechat',:post,'like','l2',:o,'2025-03-03 08:00+00',:h)"""), {"post": post, "p": friend, "o": other, "h": "h" * 64})

    result = profile_ribbon(crm_db, friend)

    assert result["timezone"] == "Asia/Shanghai"
    assert result["months"] == [
        {"month": "2025-01", "sent": 3, "received": 4, "calls": 0, "interactions": 0},
        {"month": "2025-03", "sent": 1, "received": 0, "calls": 0, "interactions": 2},
        {"month": "2025-05", "sent": 0, "received": 0, "calls": 1, "interactions": 0},
    ]
    assert profile_ribbon(crm_db, other) == {"timezone": "UTC", "months": []}
