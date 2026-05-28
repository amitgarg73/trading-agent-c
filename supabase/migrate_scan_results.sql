-- Migration: replace c_scan_results with richer schema
-- Run this if you applied schema.sql before 2026-05-27.
-- Safe to run: drops and recreates the table (it's populated fresh each day).

DROP TABLE IF EXISTS c_scan_results;

CREATE TABLE c_scan_results (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  scan_date       DATE NOT NULL,
  ticker          TEXT NOT NULL,
  technical_score INT NOT NULL,
  current_price   FLOAT NOT NULL,
  avg_volume      BIGINT,
  sector          TEXT,
  rsi             FLOAT,
  volume_ratio    FLOAT,
  atr_pct         FLOAT,
  above_sma20     BOOLEAN,
  above_sma50     BOOLEAN,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_c_scan_results_date   ON c_scan_results(scan_date DESC);
CREATE INDEX idx_c_scan_results_score  ON c_scan_results(scan_date, technical_score DESC);
