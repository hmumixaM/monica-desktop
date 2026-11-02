from app.link_matching import AccountSignals, instagram_username, linkedin_handle, score_pair


def test_handle_parsers_and_cross_platform_scores():
    assert instagram_username("https://www.instagram.com/alex.chen/") == "alex.chen"
    assert linkedin_handle("https://www.linkedin.com/in/alex-chen") == "alex-chen"
    wechat = AccountSignals(
        account_id="wx",
        provider="wechat",
        external_id="wxid_alex",
        display_name="陈艾力",
        username="Alex Chen",
        profile_id="p-wx",
        profile_name="陈艾力",
        ready=True,
        aliases=["Alex Chen", "艾力"],
        emails={"alex@example.com"},
    )
    instagram = AccountSignals(
        account_id="ig",
        provider="instagram",
        external_id="alex.chen",
        display_name="Alex Chen",
        username="alex.chen",
        profile_id="p-ig",
        profile_name="Alex Chen",
        ready=True,
        handles={"alex.chen"},
        emails={"alex@example.com"},
    )
    linkedin = AccountSignals(
        account_id="li",
        provider="linkedin",
        external_id="alex-chen",
        display_name="Alex Chen",
        username="alex-chen",
        profile_id="p-li",
        ready=True,
        aliases=["Product lead at Example"],
    )
    email_match = score_pair(instagram, wechat)
    assert email_match.score >= 99
    assert "邮箱完全一致" in email_match.reasons
    name_match = score_pair(linkedin, wechat)
    assert name_match.score >= 70
    weak = score_pair(
        AccountSignals(account_id="other", provider="instagram", external_id="zzz", display_name="zzz", username="zzz"),
        wechat,
    )
    assert weak.score < 55
