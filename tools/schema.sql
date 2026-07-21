CREATE TABLE IF NOT EXISTS simworld_schema_metadata (
  component       TEXT PRIMARY KEY,
  schema_version  INTEGER NOT NULL CHECK (schema_version > 0),
  applied_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assets (
  asset_id          TEXT PRIMARY KEY,
  qdrant_point_id  UUID NOT NULL UNIQUE,
  asset_snapshot_revision TEXT NOT NULL,
  name              TEXT NOT NULL,
  category          TEXT NOT NULL,
  subcategory       TEXT,
  source_pack       TEXT,

  setting           TEXT,
  style             TEXT,
  condition         TEXT,
  short_description TEXT,
  description       TEXT,
  "function"        TEXT,
  scene_types       TEXT[] DEFAULT '{}',
  tags              TEXT[] DEFAULT '{}',
  materials         TEXT[] DEFAULT '{}',
  mood              TEXT[] DEFAULT '{}',
  typical_placement TEXT[] DEFAULT '{}',
  affordances       TEXT[] DEFAULT '{}',
  color_palette     TEXT[] DEFAULT '{}',

  width_m           DOUBLE PRECISION,
  depth_m           DOUBLE PRECISION,
  height_m          DOUBLE PRECISION,
  footprint_w_m     DOUBLE PRECISION,
  footprint_d_m     DOUBLE PRECISION,
  bounding_radius_m DOUBLE PRECISION,
  is_symmetric      BOOLEAN,

  unreal_asset_path TEXT NOT NULL,
  asset_type        TEXT,
  mobility          TEXT,
  has_collision     BOOLEAN,
  triangle_count    INTEGER,
  lod_count         INTEGER,
  material_slots    TEXT[] DEFAULT '{}',

  caption_model     TEXT,
  render_views      TEXT[] DEFAULT '{}',
  view_count        INTEGER DEFAULT 0,
  schema_version    TEXT DEFAULT '1.0',

  embedding_version TEXT,
  embedding_hash    TEXT,
  raw_metadata      JSONB NOT NULL,

  search_tsv        TSVECTOR,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Existing v1 installations need a staged transition: ADD COLUMN cannot be
-- NOT NULL while legacy rows still exist.  The importer stamps every row and
-- finalizes this constraint after proving that the complete table matches one
-- operator-selected ASSET_SNAPSHOT_REVISION.  NOT VALID still rejects new or
-- updated rows that omit the revision during the transition.
ALTER TABLE assets
  ADD COLUMN IF NOT EXISTS asset_snapshot_revision TEXT;

CREATE INDEX IF NOT EXISTS assets_category_idx    ON assets (category);
CREATE INDEX IF NOT EXISTS assets_snapshot_revision_idx ON assets (asset_snapshot_revision);
CREATE INDEX IF NOT EXISTS assets_setting_idx     ON assets (setting);
CREATE INDEX IF NOT EXISTS assets_asset_type_idx  ON assets (asset_type);
CREATE INDEX IF NOT EXISTS assets_source_pack_idx ON assets (source_pack);
CREATE INDEX IF NOT EXISTS assets_dims_idx        ON assets (width_m, depth_m, height_m);
CREATE INDEX IF NOT EXISTS assets_tags_gin_idx        ON assets USING GIN (tags);
CREATE INDEX IF NOT EXISTS assets_scene_types_gin_idx ON assets USING GIN (scene_types);
CREATE INDEX IF NOT EXISTS assets_materials_gin_idx   ON assets USING GIN (materials);
CREATE INDEX IF NOT EXISTS assets_search_tsv_idx      ON assets USING GIN (search_tsv);

CREATE OR REPLACE FUNCTION assets_search_tsv_update() RETURNS trigger AS $$
BEGIN
  NEW.search_tsv :=
    setweight(to_tsvector('english', coalesce(NEW.name, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(NEW.subcategory, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(NEW.setting, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(NEW.style, '')), 'B') ||
    setweight(to_tsvector('english', array_to_string(NEW.tags, ' ')), 'A') ||
    setweight(to_tsvector('english', array_to_string(NEW.scene_types, ' ')), 'B') ||
    setweight(to_tsvector('english', array_to_string(NEW.materials, ' ')), 'B') ||
    setweight(to_tsvector('english', array_to_string(NEW.mood, ' ')), 'C') ||
    setweight(to_tsvector('english', array_to_string(NEW.typical_placement, ' ')), 'B') ||
    setweight(to_tsvector('english', array_to_string(NEW.affordances, ' ')), 'B') ||
    setweight(to_tsvector('english', array_to_string(NEW.color_palette, ' ')), 'C') ||
    setweight(to_tsvector('english', coalesce(NEW.condition, '')), 'C') ||
    setweight(to_tsvector('english', coalesce(NEW."function", '')), 'B') ||
    setweight(to_tsvector('english', coalesce(NEW.short_description, '')), 'B') ||
    setweight(to_tsvector('english', coalesce(NEW.description, '')), 'C');
  RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS assets_tsv_trigger ON assets;
CREATE TRIGGER assets_tsv_trigger
  BEFORE INSERT OR UPDATE ON assets
  FOR EACH ROW EXECUTE FUNCTION assets_search_tsv_update();

UPDATE assets SET search_tsv = (
  setweight(to_tsvector('english', coalesce(name, '')), 'A') ||
  setweight(to_tsvector('english', coalesce(subcategory, '')), 'A') ||
  setweight(to_tsvector('english', coalesce(setting, '')), 'A') ||
  setweight(to_tsvector('english', coalesce(style, '')), 'B') ||
  setweight(to_tsvector('english', array_to_string(tags, ' ')), 'A') ||
  setweight(to_tsvector('english', array_to_string(scene_types, ' ')), 'B') ||
  setweight(to_tsvector('english', array_to_string(materials, ' ')), 'B') ||
  setweight(to_tsvector('english', array_to_string(mood, ' ')), 'C') ||
  setweight(to_tsvector('english', array_to_string(typical_placement, ' ')), 'B') ||
  setweight(to_tsvector('english', array_to_string(affordances, ' ')), 'B') ||
  setweight(to_tsvector('english', array_to_string(color_palette, ' ')), 'C') ||
  setweight(to_tsvector('english', coalesce(condition, '')), 'C') ||
  setweight(to_tsvector('english', coalesce("function", '')), 'B') ||
  setweight(to_tsvector('english', coalesce(short_description, '')), 'B') ||
  setweight(to_tsvector('english', coalesce(description, '')), 'C')
)
WHERE asset_snapshot_revision IS NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'assets_snapshot_revision_present'
      AND conrelid = 'assets'::regclass
  ) THEN
    ALTER TABLE assets
      ADD CONSTRAINT assets_snapshot_revision_present
      CHECK (
        asset_snapshot_revision IS NOT NULL
        AND btrim(asset_snapshot_revision) <> ''
        AND length(asset_snapshot_revision) <= 160
        AND asset_snapshot_revision ~ '^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,159}$'
        AND lower(asset_snapshot_revision) NOT IN (
          'dev', 'latest', 'main', 'master', 'unknown', 'unversioned'
        )
      ) NOT VALID;
  END IF;
END
$$;

INSERT INTO simworld_schema_metadata (component, schema_version)
VALUES ('asset_catalog', 2)
ON CONFLICT (component) DO UPDATE SET
  schema_version = EXCLUDED.schema_version,
  applied_at = now();
