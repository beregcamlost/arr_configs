-- =====================================================================
-- FASE 2 — Un solo estado de conformidad.
-- Contrato unico: conforme · encolado · trabajando · aparcado · fallido
-- Todo lo demas (Emby, health, correo) lee de aqui y de ningun otro lado.
-- =====================================================================

-- Exenciones permanentes. Decision de Beren (2026-08-21): los 142 de contenedor
-- viejo y los HDR no se tocan NUNCA. Un archivo exento es "aparcado" para
-- siempre: no entra a la cola y Emby jamas lo etiqueta ni lo oculta.
CREATE TABLE IF NOT EXISTS compliance_exempt (
  media_id  INTEGER PRIMARY KEY,
  path      TEXT NOT NULL,
  motivo    TEXT NOT NULL,
  added_ts  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_compliance_exempt_path ON compliance_exempt(path);

-- Calibracion de ETA a partir del histórico real de conversion_runs.
-- La refresca `compliance calibrate`; no se escribe a mano.
CREATE TABLE IF NOT EXISTS compliance_calib (
  clase       TEXT PRIMARY KEY,   -- 'rapido' (remux/de-embed) | 'largo' (transcode)
  min_per_gb  REAL NOT NULL,
  min_floor   REAL NOT NULL,
  muestras    INTEGER,
  calc_ts     TEXT
);

DROP VIEW IF EXISTS compliance_state;
CREATE VIEW compliance_state AS
WITH lr AS (
  SELECT media_id, status, start_ts, end_ts, attempt
  FROM (
    SELECT cr.*, ROW_NUMBER() OVER (PARTITION BY cr.media_id ORDER BY cr.id DESC) rn
    FROM conversion_runs cr
  ) WHERE rn = 1
),
base AS (
  SELECT
    m.id                                   AS media_id,
    m.path                                 AS path,
    COALESCE(m.size_bytes,0)/1073741824.0  AS gb,
    cp.media_id                            AS has_plan,
    COALESCE(cp.eligible,0)                AS eligible,
    COALESCE(NULLIF(TRIM(COALESCE(cp.claimed_by,'')),''), '') AS claim,
    COALESCE(cp.reason,'')                 AS reason,
    COALESCE(cp.skip_reason,'')            AS skip_reason,
    cp.plan_ts                             AS plan_ts,
    ex.media_id                            AS exento,
    ex.motivo                              AS exento_motivo,
    lr.status                              AS run_status,
    lr.start_ts                            AS run_start,
    lr.end_ts                              AS run_end,
    COALESCE(lr.attempt,0)                 AS run_attempt
  FROM media_files m
  LEFT JOIN conversion_plan   cp ON cp.media_id = m.id
  LEFT JOIN compliance_exempt ex ON ex.media_id = m.id
  LEFT JOIN lr                   ON lr.media_id = m.id
  WHERE m.deleted_at IS NULL
),
clasificado AS (
  SELECT b.*,
    CASE
      WHEN b.exento IS NOT NULL                                   THEN 'aparcado'
      WHEN b.has_plan IS NULL                                     THEN 'encolado'
      WHEN b.eligible = 1 AND b.claim <> ''                       THEN 'trabajando'
      WHEN b.eligible = 1 AND b.run_end IS NULL AND b.run_start IS NOT NULL
           AND b.run_status NOT IN ('failed','attempt_limit_reached','swapped')
           AND julianday('now') - julianday(b.run_start) < 0.25   THEN 'trabajando'
      WHEN b.eligible = 1 AND b.run_status IN ('failed','attempt_limit_reached')
           AND b.run_attempt >= 3                                 THEN 'fallido'
      WHEN b.eligible = 1                                         THEN 'encolado'
      WHEN b.skip_reason IN ('missing_file','probe_failed','source_deleted') THEN 'fallido'
      WHEN b.skip_reason = 'already_compliant'                    THEN 'conforme'
      ELSE 'aparcado'
    END AS estado,
    CASE
      WHEN b.exento IS NOT NULL              THEN NULL
      WHEN b.has_plan IS NULL                THEN 'rapido'
      WHEN b.eligible <> 1                   THEN NULL
      WHEN b.reason IN ('deembed_only','audio_only') THEN 'rapido'
      ELSE 'largo'
    END AS clase
  FROM base b
)
SELECT
  c.media_id,
  c.path,
  c.estado,
  CASE WHEN c.estado = 'trabajando' THEN COALESCE(c.run_start, c.plan_ts) ELSE c.plan_ts END AS desde,
  CASE
    WHEN c.estado NOT IN ('encolado','trabajando') THEN NULL
    ELSE ROUND(MAX(COALESCE(cal.min_floor,2.0), c.gb * COALESCE(cal.min_per_gb,4.0)), 1)
  END AS eta_min,
  COALESCE(
    c.exento_motivo,
    NULLIF(c.reason,''),
    NULLIF(c.skip_reason,''),
    CASE WHEN c.has_plan IS NULL THEN 'sin_plan_todavia' END,
    'desconocido'
  ) AS motivo,
  c.clase,
  ROUND(c.gb,2) AS gb
FROM clasificado c
LEFT JOIN compliance_calib cal ON cal.clase = c.clase;

-- FASE 3: memoria de que etiquetamos y cuando, para el freno de seguridad de 6 h.
CREATE TABLE IF NOT EXISTS emby_tag_state (
  media_id   INTEGER PRIMARY KEY,
  emby_id    TEXT,
  tag        TEXT NOT NULL,
  path       TEXT,
  tagged_ts  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_emby_tag_state_tag ON emby_tag_state(tag);
