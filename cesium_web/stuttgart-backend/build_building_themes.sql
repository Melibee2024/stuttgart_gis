-- build_building_themes.sql
-- Materialises nexus3d.building_themes: one row per base-tile gmlid carrying its
-- footprint area, height, use category, and estimated annual rooftop-PV yield.
-- Static lookup powering the "Color by" thematic layers (use / solar) in the
-- 3D-City viewer. Rebuild after re-tiling nexus3d.citydb_base_tiles:
--   psql -f build_building_themes.sql   (≈25 s for ~32k buildings)
--
-- use_code legend (parallel to USE_LABELS in main.js):
--   0 Residential · 1 Mixed-use · 2 Commercial/Office · 3 Industrial
--   4 Public/Civic · 5 Ancillary (garage/shed/trafo) · 6 Other/Unknown
--
-- Solar model (Stuttgart, 48.78°N): annual kWh = footprint_area
--   × 0.65 usable-roof fraction (orientation, setbacks, obstructions)
--   × 0.17 kWp/m² module power density
--   × 950 kWh/kWp specific yield (PVGIS Stuttgart rooftop, mixed orientation/PR).
-- Roof area is approximated by the LoD2 footprint (flat/low-slope assumption).
--
-- This `kwh` is the FOOTPRINT-based, unshaded baseline. Two refinement passes
-- run afterwards (the API serves the most refined value available):
--   2) node build_solar_shadow.mjs  → shadow_factor (neighbour shading)
--   3) node build_solar_roof.mjs    → kwh_roof (real LoD2 roof pitch +
--                                     orientation) and final kwh_shaded.

DROP TABLE IF EXISTS nexus3d.building_themes;

CREATE TABLE nexus3d.building_themes AS
WITH faces AS (
    SELECT t.gmlid,
           t.alkis_id,
           ST_Force2D((ST_Dump(t.geom)).geom) AS f2d,
           ST_ZMax(t.geom) - ST_ZMin(t.geom)  AS hgt
    FROM nexus3d.citydb_base_tiles t
    WHERE t.geom IS NOT NULL
),
fp AS (
    SELECT gmlid,
           MAX(alkis_id)                                          AS alkis_id,
           ST_Area(ST_UnaryUnion(ST_MakeValid(ST_Collect(f2d))))  AS area_m2,
           MAX(hgt)                                               AS hgt
    FROM faces
    GROUP BY gmlid
)
SELECT
    fp.gmlid,
    fp.alkis_id,
    round(fp.area_m2::numeric, 1)                        AS area_m2,
    round(fp.hgt::numeric, 1)                            AS hgt,
    round((fp.area_m2 * 0.65 * 0.17 * 950)::numeric, 0)::int AS kwh,
    CASE
        WHEN dfv.alkis_usage ILIKE 'Wohn- und %' THEN 1
        WHEN dfv.alkis_usage ILIKE 'Wohn%'       THEN 0
        WHEN dfv.alkis_usage ~* 'Garage|Schuppen|Gartenhaus|Carport|Nebengeb|Gew.chshaus|Unterstand|Trafo|Umform|Pumpwerk' THEN 5
        WHEN dfv.alkis_usage ~* 'Gesch.ft|B.ro|Verwaltung|Handel|Markt|Kaufhaus|Bank|Hotel|Gastst|Veranstaltung|Messe|Parkhaus' THEN 2
        WHEN dfv.alkis_usage ~* 'Betrieb|Fabrik|Werkstatt|Industrie|Lager|Vorrat|Produktion|Werk|Tankstelle' THEN 3
        WHEN dfv.alkis_usage ~* 'Schule|Hochschul|Universit|Krankenhaus|Klinik|Kirche|Kapelle|Kinder|Gemeinde|Rathaus|Bibliothek|Museum|Sport|Feuerwehr|Polizei|Bahnhof|Gericht|Amt|Theater|Friedhof|.ffentlich' THEN 4
        ELSE 6
    END                                                  AS use_code,
    dfv.alkis_usage                                      AS alkis_usage
FROM fp
LEFT JOIN public.data_fusion_view dfv ON dfv.gmlid = fp.alkis_id;

ALTER TABLE nexus3d.building_themes ADD PRIMARY KEY (gmlid);

SELECT count(*) AS rows,
       round(sum(kwh)::numeric / 1e6, 1) AS total_gwh
FROM nexus3d.building_themes;
