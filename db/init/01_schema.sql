--
-- PostgreSQL database dump
--

\restrict ezb9t6hG6gpMQzu8xQ8F2ulbLqxvADB1dWCAwIkyidKrjAYUqbqAdg8uid7HPr2

-- Dumped from database version 16.10 (Debian 16.10-1.pgdg13+1)
-- Dumped by pg_dump version 16.10 (Debian 16.10-1.pgdg13+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: pgcrypto; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;


--
-- Name: EXTENSION pgcrypto; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pgcrypto IS 'cryptographic functions';


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: forecast_hourly; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.forecast_hourly (
    id bigint NOT NULL,
    model_name text NOT NULL,
    ts_forecast timestamp with time zone NOT NULL,
    ts_valid timestamp with time zone NOT NULL,
    latitude double precision NOT NULL,
    longitude double precision NOT NULL,
    snowfall_cm numeric,
    source text DEFAULT 'open-meteo'::text,
    station_triplet text
);


--
-- Name: forecast_hourly_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.forecast_hourly_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: forecast_hourly_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.forecast_hourly_id_seq OWNED BY public.forecast_hourly.id;


--
-- Name: snotel_daily_raw; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.snotel_daily_raw (
    station_triplet text NOT NULL,
    date date NOT NULL,
    snwd_in double precision,
    wteq_in double precision,
    prec_in double precision,
    source text
);


--
-- Name: snotel_daily_new_snow_cm; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.snotel_daily_new_snow_cm AS
 SELECT station_triplet,
    date,
    (GREATEST((0)::double precision, (snwd_in - lag(snwd_in) OVER (PARTITION BY station_triplet ORDER BY date))) * (2.54)::double precision) AS new_snow_cm
   FROM public.snotel_daily_raw;


--
-- Name: snotel_daily_precip_cm; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.snotel_daily_precip_cm AS
 SELECT station_triplet,
    date,
    (GREATEST((0)::double precision, (prec_in - lag(prec_in) OVER (PARTITION BY station_triplet ORDER BY date))) * (2.54)::double precision) AS precip_cm_daily
   FROM public.snotel_daily_raw;


--
-- Name: snotel_daily_swe_gain_cm; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.snotel_daily_swe_gain_cm AS
 SELECT station_triplet,
    date,
    (GREATEST((0)::double precision, (wteq_in - lag(wteq_in) OVER (PARTITION BY station_triplet ORDER BY date))) * (2.54)::double precision) AS swe_gain_cm
   FROM public.snotel_daily_raw;


--
-- Name: snotel_daily_accums_cm; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.snotel_daily_accums_cm AS
 SELECT r.station_triplet,
    r.date,
    n.new_snow_cm,
    s.swe_gain_cm,
    p.precip_cm_daily
   FROM (((( SELECT DISTINCT snotel_daily_raw.station_triplet,
            snotel_daily_raw.date
           FROM public.snotel_daily_raw) r
     LEFT JOIN public.snotel_daily_new_snow_cm n USING (station_triplet, date))
     LEFT JOIN public.snotel_daily_swe_gain_cm s USING (station_triplet, date))
     LEFT JOIN public.snotel_daily_precip_cm p USING (station_triplet, date));


--
-- Name: snotel_daily_obs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.snotel_daily_obs (
    station_triplet text NOT NULL,
    date date NOT NULL,
    snow_depth_cm double precision,
    swe_cm double precision,
    precip_cm double precision,
    source text DEFAULT 'awdb'::text
);


--
-- Name: snotel_hourly_obs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.snotel_hourly_obs (
    station_triplet text NOT NULL,
    ts timestamp with time zone NOT NULL,
    snowfall_cm double precision,
    snow_depth_cm double precision,
    source text DEFAULT 'awdb'::text
);


--
-- Name: snotel_station; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.snotel_station (
    station_triplet text NOT NULL,
    name text NOT NULL,
    latitude double precision NOT NULL,
    longitude double precision NOT NULL,
    elevation_m double precision
);


--
-- Name: v_forecast_vs_actual; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_forecast_vs_actual AS
 SELECT f.station_triplet,
    s.name AS station_name,
    f.model_name,
    f.ts_forecast,
    f.ts_valid,
    f.snowfall_cm AS fc_snow_cm,
    o.snowfall_cm AS ob_snow_cm,
    (EXTRACT(epoch FROM (f.ts_valid - f.ts_forecast)) / 3600.0) AS lead_hours,
    ((f.snowfall_cm)::double precision - o.snowfall_cm) AS error_cm
   FROM ((public.forecast_hourly f
     JOIN public.snotel_station s ON ((s.station_triplet = f.station_triplet)))
     JOIN public.snotel_hourly_obs o ON (((o.station_triplet = f.station_triplet) AND (o.ts = f.ts_valid))));


--
-- Name: v_forecast_with_station; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_forecast_with_station AS
 SELECT f.model_name,
    f.ts_forecast,
    f.ts_valid,
    f.latitude,
    f.longitude,
    f.snowfall_cm,
    f.source,
    f.station_triplet,
    s.name AS station_name,
    s.latitude AS station_lat,
    s.longitude AS station_lon,
    s.elevation_m
   FROM (public.forecast_hourly f
     LEFT JOIN public.snotel_station s ON ((s.station_triplet = f.station_triplet)));


--
-- Name: forecast_hourly id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.forecast_hourly ALTER COLUMN id SET DEFAULT nextval('public.forecast_hourly_id_seq'::regclass);


--
-- Name: forecast_hourly forecast_hourly_model_name_ts_forecast_ts_valid_latitude_lo_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.forecast_hourly
    ADD CONSTRAINT forecast_hourly_model_name_ts_forecast_ts_valid_latitude_lo_key UNIQUE (model_name, ts_forecast, ts_valid, latitude, longitude);


--
-- Name: forecast_hourly forecast_hourly_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.forecast_hourly
    ADD CONSTRAINT forecast_hourly_pkey PRIMARY KEY (id);


--
-- Name: snotel_daily_obs snotel_daily_obs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.snotel_daily_obs
    ADD CONSTRAINT snotel_daily_obs_pkey PRIMARY KEY (station_triplet, date);


--
-- Name: snotel_daily_raw snotel_daily_raw_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.snotel_daily_raw
    ADD CONSTRAINT snotel_daily_raw_pkey PRIMARY KEY (station_triplet, date);


--
-- Name: snotel_hourly_obs snotel_hourly_obs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.snotel_hourly_obs
    ADD CONSTRAINT snotel_hourly_obs_pkey PRIMARY KEY (station_triplet, ts);


--
-- Name: snotel_station snotel_station_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.snotel_station
    ADD CONSTRAINT snotel_station_pkey PRIMARY KEY (station_triplet);


--
-- Name: forecast_hourly_station_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX forecast_hourly_station_idx ON public.forecast_hourly USING btree (station_triplet);


--
-- Name: forecast_hourly_valid_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX forecast_hourly_valid_idx ON public.forecast_hourly USING btree (ts_valid DESC);


--
-- Name: forecast_model_run_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX forecast_model_run_idx ON public.forecast_hourly USING btree (model_name, ts_forecast);


--
-- Name: snotel_hourly_obs_ts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX snotel_hourly_obs_ts_idx ON public.snotel_hourly_obs USING btree (ts);


--
-- Name: snotel_station_latlon_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX snotel_station_latlon_idx ON public.snotel_station USING btree (latitude, longitude);


--
-- Name: forecast_hourly forecast_station_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.forecast_hourly
    ADD CONSTRAINT forecast_station_fk FOREIGN KEY (station_triplet) REFERENCES public.snotel_station(station_triplet);


--
-- PostgreSQL database dump complete
--

\unrestrict ezb9t6hG6gpMQzu8xQ8F2ulbLqxvADB1dWCAwIkyidKrjAYUqbqAdg8uid7HPr2

