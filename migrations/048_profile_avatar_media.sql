-- One definition of "the avatar to show" for list rows: the chosen profile
-- avatar, otherwise the newest platform avatar linked to the profile.
CREATE OR REPLACE FUNCTION profile_avatar_media(target_profile uuid, chosen uuid) RETURNS uuid
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(chosen, (
    SELECT l.media_id FROM media_links l JOIN media_assets m ON m.id = l.media_id
    WHERE l.entity_type = 'profile' AND l.entity_id = target_profile
      AND l.role IN ('wechat_avatar', 'linkedin_avatar', 'instagram_avatar')
    ORDER BY m.created_at DESC LIMIT 1))
$$;
