-- Migration 011: add slovak_polish column to v_segment
-- Adds the polish source (code='polish', authority_rank=85) to the read view.
-- Also promotes translation_status and reviewer_notes into the view
-- (they live on segment directly; no aggregation needed).
DROP VIEW IF EXISTS v_segment;
CREATE VIEW v_segment AS
  SELECT
    s.segment_id,
    s.work_id,
    s.locator_path,
    s.element_type,
    s.reply_to,
    s.translation_status,
    s.reviewer_notes,
    max(t.content) FILTER (WHERE t.lang='la')                         AS latin,
    max(t.content) FILTER (WHERE t.lang='cs')                         AS czech,
    max(t.content) FILTER (WHERE t.lang='en')                         AS english,
    max(t.content) FILTER (WHERE t.lang='sk' AND src.code='model')    AS slovak_draft,
    max(t.content) FILTER (WHERE t.lang='sk' AND src.code='polish')   AS slovak_polish,
    max(t.content) FILTER (WHERE t.lang='sk' AND src.code='human')    AS slovak_final
  FROM segment s
  JOIN segment_text t   USING (segment_id)
  JOIN source     src   ON t.source_id = src.source_id
  GROUP BY s.segment_id, s.work_id, s.locator_path,
           s.element_type, s.reply_to,
           s.translation_status, s.reviewer_notes;
