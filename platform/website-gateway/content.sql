-- Synthetic imported-style WordPress post and old-URL redirect, as the reviewed
-- importer would leave them after `apply --publish`. Test fixture only.
BEGIN;
INSERT INTO posts (slug, title, excerpt, body_html, author_name, published_at, wp_id, wp_guid, wp_status, _status)
VALUES ('synthetic-imported-post', 'Synthetic imported post', 'Synthetic imported excerpt.',
        '<p>Synthetic imported body.</p><p><img src="https://cms.terrace.invalid/api/media/file/public.png" alt="Synthetic public"><img src="https://elsewhere.invalid/tracker.png" alt="Synthetic foreign"></p>',
        'Synthetic Author', now() - interval '1 day', 424242, 'https://old.terrace.invalid/?p=424242', 'publish', 'published');
INSERT INTO _posts_v (parent_id, version_slug, version_title, version_excerpt, version_body_html, version_author_name,
                      version_published_at, version_wp_id, version_wp_guid, version_wp_status, version_updated_at,
                      version_created_at, version__status, latest)
SELECT id, slug, title, excerpt, body_html, author_name, published_at, wp_id, wp_guid, wp_status, updated_at, created_at,
       _status::text::enum__posts_v_version_status, true
FROM posts WHERE slug = 'synthetic-imported-post';
INSERT INTO redirects (source_path, destination_url, status_code, active, wp_id)
VALUES ('/2019/05/synthetic-imported-post', '/news/synthetic-imported-post/', '301', true, 424242);
COMMIT;
