-- Drop ALL TABLES (reverse dependency order)

DROP TABLE if exists kraken2_lcas CASCADE;
DROP TABLE if exists kraken2_report CASCADE;
DROP TABLE if exists taxtriage_report CASCADE;
DROP TABLE if exists taxtriage_run CASCADE;
DROP TABLE if exists wilder_results CASCADE;
DROP TABLE if exists mbon_samples CASCADE;
DROP TABLE if exists bioinformatics CASCADE;
DROP TABLE if exists data_management CASCADE;
DROP TABLE if exists sample_summary CASCADE;
DROP TABLE if exists run_summary CASCADE;
DROP TABLE if exists basecall_run CASCADE;
DROP TABLE if exists run CASCADE;
DROP TABLE if exists sample CASCADE;
DROP TABLE if exists collection CASCADE;
DROP TABLE if exists library_preparation_sequencing CASCADE;
DROP TABLE if exists pcr CASCADE;
DROP TABLE if exists targeted_assay_detection CASCADE;
DROP TABLE if exists location CASCADE;
DROP TABLE if exists instrument CASCADE;
DROP TABLE if exists taxonomy CASCADE;
DROP TABLE if exists group_membership CASCADE;
DROP TABLE if exists project CASCADE;
DROP TABLE if exists users CASCADE;
DROP TABLE if exists institution CASCADE;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1) Institutions
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE institution (
    institution_id SERIAL PRIMARY KEY,
    institution_name TEXT NOT NULL UNIQUE
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 2) Users
--    "user" is a reserved word; using "users" to avoid quoting.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE users (
    id            BIGSERIAL PRIMARY KEY,
    email         TEXT,
    full_name     TEXT,
    is_admin      BOOLEAN NOT NULL DEFAULT FALSE,
    db_role       TEXT,
    username      TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE group_membership (
    membership_id  SERIAL PRIMARY KEY,
    institution_id INT NOT NULL
        REFERENCES institution(institution_id) ON DELETE CASCADE,
    id INT NOT NULL
        REFERENCES users(id) ON DELETE CASCADE,
    group_name TEXT NOT NULL,
    -- Prevent duplicate memberships of the same user in the same group at the same institution
    UNIQUE (institution_id, id, group_name)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 3) Project
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE project (
    project_id        SERIAL PRIMARY KEY,
    recorded_by       INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_contact   TEXT NOT NULL,
    institution_id    INT REFERENCES institution(institution_id) ON DELETE CASCADE,
    project_name      TEXT,
    parent_project_id INT,
    study_factor      TEXT,
    assay_type        TEXT NOT NULL,
    sterilise_method  TEXT,
    neg_cont_0_1      TEXT,
    pos_cont_0_1      TEXT
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 4) Collection  (NEW — sits between Project and Sample)
--    Represents a discrete field event: a cruise, an outing, a lab batch, etc.
--    A project contains many collections; each collection groups related samples.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE collection (
    collection_id   SERIAL PRIMARY KEY,
    project_id      INT NOT NULL REFERENCES project(project_id) ON DELETE CASCADE,
    collection_name TEXT,                            -- e.g. "Cruise_071825", "Wilder_Batch_01"
    collection_type TEXT,                            -- e.g. 'cruise', 'survey', 'lab_batch', 'outing'
    collected_at    TIMESTAMPTZ,                     -- representative start date/time of event
    description     TEXT,
    comments        TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 5) Instrument
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE instrument (
    instrument_id   SERIAL PRIMARY KEY,
    platform        TEXT NOT NULL,        -- OXFORD, ILLUMINA, PACBIO, etc.
    instrument_name TEXT NOT NULL         -- MinION, PromethION, MiSeq, NextSeq, etc.
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 6) Location
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE location (
    location_id    SERIAL PRIMARY KEY,
    location_alias TEXT NOT NULL UNIQUE,  -- e.g. "Mid Atlantic (Coastal MD)"
    latitude       FLOAT,
    longitude      FLOAT,
    altitude       BIGINT,
    depth          TEXT,
    project_id     INT REFERENCES project(project_id),
    description    TEXT,
    comments       TEXT
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 7) Sample  (physical specimen — now linked to a collection, NOT a run)
--    NOTE: basecall_id is stored as a plain INT here to avoid a circular
--    dependency (sample → basecall_run → run → sample).  A FK can be enforced
--    at the application level or added later via ALTER TABLE if needed.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE sample (
    sample_id            SERIAL PRIMARY KEY,
    collection_id        INT REFERENCES collection(collection_id),   -- ← NEW (was run_id)
    submitted_by         INT REFERENCES users(id),
    instrument_id        INT REFERENCES instrument(instrument_id),
    basecall_id          INT,                                        -- soft ref to basecall_run
    completed_at         TIMESTAMPTZ,
    collected_at         TIMESTAMPTZ,
    directory            TEXT,
    filepath             TEXT,
    location_id          INT REFERENCES location(location_id),
    sample_type          TEXT CHECK (sample_type IN (
                             'SAMPLE', 'POSITIVE_CONTROL', 'NEGATIVE_CONTROL', 'OTHER')),
    sample_name          TEXT,
    sample_collect_method TEXT
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 8) Run  (sequencing or other processing performed ON a specific sample)
--    A run now belongs to a sample, not directly to a project.
--    Traverse sample → collection → project to reach the project.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE run (
    run_id        SERIAL PRIMARY KEY,
    sample_id     INT REFERENCES sample(sample_id),   -- ← NEW (was project_id)
    directory     TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    entered_by    INT REFERENCES users(id),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    comments      TEXT,
    instrument_id INT REFERENCES instrument(instrument_id)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 9) Basecall Run  (links back to a run)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE basecall_run (
    basecall_id        SERIAL PRIMARY KEY,
    run_id             INT REFERENCES run(run_id),
    basecaller_software TEXT,
    basecaller_version  TEXT,
    basecaller_model    TEXT CHECK (basecaller_model IN (
                            'HAC', 'SUP', 'OTHER', 'FAST', 'NONE', 'BCL')),
    basecalled_at       TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 10) Library Preparation / Sequencing metadata
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE library_preparation_sequencing (
    id             SERIAL PRIMARY KEY,
    project_id     INT REFERENCES project(project_id),
    barcoding_pcr_appr TEXT,
    pcr2_thermocycler  TEXT,
    platform           TEXT,
    instrument         TEXT,
    seq_kit            TEXT
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 11) Summary tables
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE sample_summary (
    summary_id    SERIAL PRIMARY KEY,
    sample_id     INT REFERENCES sample(sample_id),
    total_reads   INT,
    total_taxa    INT,
    avg_err       FLOAT,
    median_err    FLOAT,
    passed_reads  INT,
    failed_reads  INT,
    unique_taxa   TEXT[] NOT NULL DEFAULT '{}'
);

CREATE TABLE run_summary (
    summary_id   SERIAL PRIMARY KEY,
    run_id       INT REFERENCES run(run_id),
    total_reads  INT,
    total_taxa   INT,
    unique_taxa  TEXT[] NOT NULL DEFAULT '{}'
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 12) Data management and Bioinformatics
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE data_management (
    id                      SERIAL PRIMARY KEY,
    run_id               INT REFERENCES run(run_id),
    checkls_ver             TEXT,
    mod_date                DATE,
    license                 TEXT,
    rightsHolder            TEXT,
    accessRights            TEXT,
    developers              TEXT,
    informationWithheld     TEXT,
    dataGeneralizations     TEXT,
    bibliographicCitation   TEXT,
    associated_resource     TEXT,
    code_repo               TEXT
);

-- Bioinformatics table: one entry per pipeline run, linked to data_management
CREATE TABLE bioinformatics (
    id                  SERIAL PRIMARY KEY,
    data_management_id  INT REFERENCES data_management(id),
    sop_bioinformatics  TEXT,
    version_pipeline    TEXT,
    last_run            TIMESTAMPTZ
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 13) Taxonomy
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE taxonomy (
    organism_id     SERIAL PRIMARY KEY,
    organism_name   TEXT NOT NULL,
    taxonomic_id    INT NOT NULL,
    taxonomic_rank  TEXT NOT NULL
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 14) Bioinformatic pipeline tables
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE taxtriage_run (
    taxtriage_run_id SERIAL PRIMARY KEY,
    bioinfo_id       INT REFERENCES bioinformatics(id),
    database         TEXT NOT NULL,
    run_location     TEXT NOT NULL,
    run_date         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    krona_location   TEXT NOT NULL
);

CREATE TABLE taxtriage_report (
    taxtriage_report_id SERIAL PRIMARY KEY,
    taxtriage_run_id    INT REFERENCES taxtriage_run(taxtriage_run_id),
    bioinfo_id          INT REFERENCES bioinformatics(id),
    tass_score          FLOAT,
    reads_aligned       INT,
    organism_detected   TEXT,
    taxid               INT,
    specimen_id         TEXT,
    specimen_type       TEXT,
    pct_reads_aligned   FLOAT,
    coverage            FLOAT,
    k2_reads            BIGINT
);

CREATE TABLE kraken2_report (
    id              SERIAL PRIMARY KEY,
    bioinfo_id      INT REFERENCES bioinformatics(id),
    database        TEXT NOT NULL,
    report_date     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    krona_location  TEXT NOT NULL,
    report_location TEXT NOT NULL
);

CREATE TABLE kraken2_lcas (
    kraken2_lcas_id    SERIAL PRIMARY KEY,
    taxonomy_id        INT,
    parent_taxonomy_id INT,
    kraken2_report_id  INT REFERENCES kraken2_report(id),
    rank               TEXT NOT NULL,
    abundance          NUMERIC NOT NULL,
    count              INT NOT NULL,
    name               TEXT NOT NULL
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 15) MBON-specific tables
-- ─────────────────────────────────────────────────────────────────────────────

-- mbon_samples: MBON-specific metadata per physical sample
DROP TABLE IF EXISTS mbon_samples CASCADE;
CREATE TABLE mbon_samples (
    mbon_data_id                SERIAL PRIMARY KEY,
    sample_id                   INT REFERENCES sample(sample_id),
    mbon_sample_id              INT,
    mbon_samp_alias             TEXT,
    sampling_method             TEXT,
    material_method_id          TEXT,
    filtration_method           TEXT,
    cruise_number               TEXT,
    samp_category               TEXT,
    geo_loc_name                TEXT,
    neg_cont_type TEXT CHECK (
        neg_cont_type IN (
            'site negative',
            'field negative',
            'process negative',
            'extraction negative',
            'PCR negative',
            'other:'
        )
    ),
    pos_cont_type               TEXT,
    extraction_method           TEXT,
    sample_storage              TEXT,
    sample_depth                TEXT,
    station_id                  TEXT,
    cruise_id                   BIGINT,
    location_id                 INT REFERENCES location(location_id),
    rel_neg_id                  INT REFERENCES sample(sample_id),
    rel_pos_id                  INT REFERENCES sample(sample_id),
    sea_surface_temperature     FLOAT,
    station                     TEXT,
    salinity                    FLOAT,
    temp                        FLOAT,
    elev                        FLOAT,
    latitude                    FLOAT,
    longitude                   FLOAT,
    water_volume_filtered       TEXT,
    filter_pore_size            FLOAT,
    env_broad_scale             TEXT,
    env_local_scale             TEXT,
    env_medium                  TEXT,
    habitat_natural_artificial_0_1 TEXT CHECK (
        habitat_natural_artificial_0_1 IN ('0', '1')
    ),
    samp_collect_method         TEXT,
    samp_collect_device         TEXT,
    samp_size                   TEXT,
    samp_size_unit TEXT CHECK (
        samp_size_unit IN ('mL', 'L', 'mg', 'g', 'kg', 'cm2', 'm2', 'cm3', 'm3', 'other:')
    ),
    samp_store_temp             TEXT,
    samp_store_sol TEXT CHECK (
        samp_store_sol IN ('ethanol','sodium acetate','longmire','lysis buffer','none','other:')
    ),
    samp_store_dur              TEXT,
    samp_store_loc              TEXT,
    dna_store_loc               TEXT,
    samp_store_method_additional TEXT,
    samp_mat_process            TEXT,
    filter_passive_active_0_1 TEXT CHECK (
        filter_passive_active_0_1 IN ('0','1')
    ),
    stationed_sample_dur        TEXT,
    pump_flow_rate              TEXT,
    pump_flow_rate_unit TEXT CHECK (
        pump_flow_rate_unit IN ('m3/s','m3/min','m3/h','L/s','L/min','L/h','other:')
    ),
    prefilter_material          TEXT,
    size_frac_low               TEXT,
    size_frac                   FLOAT,
    filter_diameter             TEXT,
    filter_surface_area         TEXT,
    filter_material TEXT CHECK (
        filter_material IN (
            'cellulose','cellulose ester','glass fiber','thermoplastic membrane',
            'track etched polycarbonate','nylon','polyethersulfone','other:'
        )
    ),
    filter_name                 TEXT,
    precip_chem_prep TEXT CHECK (
        precip_chem_prep IN ('ethanol','isopropanol','sodium chloride','other:')
    ),
    precip_force_prep           TEXT,
    precip_time_prep            TEXT,
    precip_temp_prep            TEXT,
    prepped_samp_store_temp     TEXT,
    prepped_samp_store_sol TEXT CHECK (
        prepped_samp_store_sol IN ('ethanol','sodium acetate','longmire','lysis buffer','none','other:', 'DNA/RNA Shield')
    ),
    prepped_samp_store_dur      TEXT,
    prep_method_additional      TEXT,
    date_ext                    TEXT,
    samp_vol_we_dna_ext         TEXT,
    samp_vol_we_dna_ext_unit TEXT CHECK (
        samp_vol_we_dna_ext_unit IN ('mL','L','mg','g','kg','cm2','m2','cm3','m3','other:')
    ),
    nucl_acid_ext_lysis TEXT CHECK (
        nucl_acid_ext_lysis IN ('physical','chemical','enzymatic','thermal','osmotic','other:')
    ),
    nucl_acid_ext_sep TEXT CHECK (
        nucl_acid_ext_sep IN (
            'column-based','magnetic beads','centrifugation','precipitation',
            'phenol chloroform','gel electrophoresis','other:'
        )
    ),
    nucl_acid_ext               TEXT,
    nucl_acid_ext_kit           TEXT,
    nucl_acid_ext_modify        TEXT,
    dna_cleanup_0_1 TEXT CHECK (
        dna_cleanup_0_1 IN ('0','1')
    ),
    dna_cleanup_method          TEXT,
    concentration               TEXT,
    concentration_unit TEXT CHECK (
        concentration_unit IN ('ng/µl','copies/µl','other:')
    ),
    concentration_method        TEXT,
    ratioOfAbsorbance260_280    TEXT,
    pool_dna_num                TEXT,
    nucl_acid_ext_method_additional TEXT,
    assay_name                  TEXT,
    detected_notDetected TEXT CHECK (
        detected_notDetected IN ('0','1')
    ),
    samp_weather                TEXT,
    minimumDepthInMeters        TEXT,
    maximumDepthInMeters        TEXT,
    tot_depth_water_col         FLOAT
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 16) Wilderlab eDNA results
-- ─────────────────────────────────────────────────────────────────────────────
DROP TABLE IF EXISTS wilder_results CASCADE;
CREATE TABLE wilder_results (
    wilder_result_id  SERIAL PRIMARY KEY,
    mbon_data_id      INT REFERENCES mbon_samples(mbon_data_id),
    bioinfo_id        INT REFERENCES bioinformatics(id),
    wilder_job_id     TEXT,
    wilder_uid        TEXT,
    scientific_name   TEXT NOT NULL,
    taxonomic_rank    TEXT NOT NULL,
    tax_id            INT,
    common_name       TEXT,
    organism_group    TEXT,
    taxonomy_id       INT REFERENCES taxonomy(organism_id),
    read_count        BIGINT NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for wilder_results
CREATE INDEX idx_wilder_results_sample       ON wilder_results (mbon_data_id);
CREATE INDEX idx_wilder_results_bioinfo      ON wilder_results (bioinfo_id);
CREATE INDEX idx_wilder_results_taxonomy     ON wilder_results (taxonomy_id);
CREATE INDEX idx_wilder_results_tax_id       ON wilder_results (tax_id);
CREATE INDEX idx_wilder_results_job          ON wilder_results (wilder_job_id);
CREATE INDEX idx_wilder_results_sample_taxon ON wilder_results (mbon_data_id, tax_id);

CREATE UNIQUE INDEX idx_wilder_results_unique_entry
    ON wilder_results (wilder_job_id, wilder_uid, tax_id, taxonomic_rank);

COMMENT ON TABLE wilder_results IS
    'Per-sample, per-taxon ASV read counts from Wilderlab eDNA sequencing results (aggregated tab).';
COMMENT ON COLUMN wilder_results.wilder_job_id IS
    'Wilderlab batch/job identifier, e.g. WLM611808';
COMMENT ON COLUMN wilder_results.wilder_uid IS
    'Wilderlab sample UID (column header in the aggregated spreadsheet), e.g. 571301';
COMMENT ON COLUMN wilder_results.organism_group IS
    'Wilderlab organism grouping, e.g. Crustaceans, Fish, Diatoms, Cnidarians';
COMMENT ON COLUMN wilder_results.read_count IS
    'ASV read count for this taxon in this sample from the aggregated tab';

-- ─────────────────────────────────────────────────────────────────────────────
-- 17) Helpful indexes
-- ─────────────────────────────────────────────────────────────────────────────
CREATE INDEX idx_collection_project    ON collection (project_id);
CREATE INDEX idx_sample_collection     ON sample (collection_id);
CREATE INDEX idx_run_sample            ON run (sample_id);
CREATE INDEX idx_group_membership_institution_group ON group_membership (institution_id, group_name);
CREATE INDEX idx_group_membership_user ON group_membership (id);
