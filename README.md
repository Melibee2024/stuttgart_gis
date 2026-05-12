# HFT Stuttgart 3D and 2D Data Integration
This repository contains the database configurations, SQL logic, and synchronization workflows for the HFT Stuttgart Digital Twin project. It facilitates the integration of authoritative 2D ALKIS cadastral data, 3D CityGML models, and dynamic field observations into a unified PostgreSQL/PostGIS environment.

## Project Structure
* sql_scripts/: PostgreSQL/PostGIS schemas, views, and relational logic.

  * `schemas/`: Definitions for `stuttgart_2d`, `citydb`, and `qfield_data`.

  * `views/`: Triple-schema joins linking 3D, 2D, and field data.

* py_updates/: Python-based automation for data processing and database maintenance.

* qgis_templates/: Pre-configured QGIS project files (.qgs) optimized for desktop analysis and mobile packaging.

* cesium_web/: CesiumJS integration files, including HTML/JavaScript for the web-based 3D visualization.

* media/: Storage for field-captured photographic evidence and external URLs for the web viewer.

* database_configs/: Metadata and session configurations for PyCharm Professional database tools.

## General Logic & Workflow
1. Environment Setup: Implementation of a PostGIS-enabled database with dedicated schemas for 2D, 3D, and field data. The 2D schema is initialized via norGIS ALKIS import (gid7) and the 3D schema via 3DCityDB.

2. ETL & Preprocessing: Extraction and loading of six LGL-BW CityGML tiles and ALKIS datasets. SQL functions (e.g., ST_IsValid) are utilized to ensure geometric integrity before attribute pruning and optimization.

3. Mobile GIS & Field Survey: Configuration of QGIS projects for QField, including the creation of the fk_gmlid relational field and custom attribute collection forms. Field data (usage, condition, photos) is captured within a 1 km buffer of the HFT campus.

4. Data Fusion: Establishing a relational link across the three schemas via SQL views. This synchronizes citydb.building geometries with stuttgart_2d.ax_gebaeude records and QField survey results using the GMLID as a primary key.

5. Visualization: Transition to a browser-based Digital Twin using CesiumJS. SQL views are served to the web environment, allowing users to query 3D buildings and dynamically retrieve synchronized 2D history and field photos.
---
## Configuration & Setup
Database: PostgreSQL/PostGIS. Connection parameters should be configured within the PyCharm Database tool window or a local .env file.

External Tools:

* 3DCityDB Importer/Exporter: For CityGML tile management.

* norGIS ALKIS-Import (gid7): For GeoInfoDok 7 cadastral data processing.

* Environment: Requires Python 3.x and a local web server for hosting the CesiumJS frontend.
