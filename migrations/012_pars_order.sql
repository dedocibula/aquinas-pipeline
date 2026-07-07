-- Migration 012: pars_order lookup table
--
-- Stores the canonical display ordinal for each pars label within a work.
-- Queries JOIN this table to order pars correctly without hardcoding label
-- names in application code.  Seed data here is Summa Theologiae (work_id=1).
-- Any future work row would have its own INSERT.

CREATE TABLE pars_order (
    work_id    integer NOT NULL REFERENCES work(work_id),
    pars_label text    NOT NULL,
    ordinal    integer NOT NULL,
    PRIMARY KEY (work_id, pars_label)
);

-- Summa Theologiae canonical pars sequence: I → I-II → II-II → III
-- (ltree stores '-' as '_' in labels)
INSERT INTO pars_order (work_id, pars_label, ordinal) VALUES
    (1, 'I',     1),
    (1, 'I_II',  2),
    (1, 'II_II', 3),
    (1, 'III',   4);
