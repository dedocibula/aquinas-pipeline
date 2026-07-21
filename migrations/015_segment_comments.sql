-- STOP: human review required before running.
-- Adds editor-internal threaded comments per segment + per-(user,segment) read/notify state.

-- Flat thread per segment; resolution is thread-level.
-- author/created_at -> sidebar + timeline + digest; resolved* -> thread state / open-count badge.
CREATE TABLE segment_comment (
    comment_id   serial      PRIMARY KEY,
    segment_id   integer     NOT NULL REFERENCES segment(segment_id),
    author       text        NOT NULL,               -- editor email (plain text, like human_reviewed_by)
    body         text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    resolved     boolean     NOT NULL DEFAULT false,
    resolved_by  text        NULL,
    resolved_at  timestamptz NULL
);
CREATE INDEX segment_comment_segment_idx ON segment_comment (segment_id, created_at);

-- Per-(user,segment) watermarks.
-- last_read_at  -> in-app unread dot + digest filter (bumped when the user opens the thread).
-- last_notified_at -> digest de-dupe (bumped when a digest covering these comments is sent).
CREATE TABLE comment_thread_state (
    segment_id       integer     NOT NULL REFERENCES segment(segment_id),
    user_email       text        NOT NULL,
    last_read_at     timestamptz NULL,
    last_notified_at timestamptz NULL,
    PRIMARY KEY (segment_id, user_email)
);
