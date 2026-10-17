# LNMP Developer & Contributor Guide

This guide details how to set up a local development environment for the Network Monitoring Platform (LNMP) v3.1.35s (Enterprise LTS) and outlines standard contribution procedures.

## 1. Project Structure

The project is split into two primary domains:
- `/backend`: Python 3.10+, FastAPI, SQLAlchemy 2.0 (Async), Pydantic Settings, Alembic, Redis 6+ (optional acceleration).
- `/frontend`: Node.js, Vue 3 (Composition API), Vite, PrimeVue.

## 2. Local Environment Setup

### Database Requirement
You must have an instance of PostgreSQL 14+ running locally with the **TimescaleDB** extension installed.
```sql
-- Connect to your local postgres instance and create the database
CREATE DATABASE netmon;
\c netmon
CREATE EXTENSION IF NOT EXISTS timescaledb;
```

### Backend (Python/FastAPI)

1. **Virtual Environment:**
   Navigate to the `backend/` directory, create a virtual environment, and install dependencies.
   ```bash
   cd backend
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Environment Variables:**
   Copy `.env.example` to `.env` and configure your local database credentials.
   ```bash
   cp .env.example .env
   # Edit .env to match your local postgres setup
   ```

3. **Database Migrations:**
   Apply the Alembic schema migrations to build the tables and TimescaleDB hypertables.
   ```bash
   alembic upgrade head
   ```
   *Note: Ensure the `alembic.ini` file or your `.env` is correctly pointing to your local database.*

4. **Run the Development Server:**
   Start the FastAPI app with Uvicorn (hot-reloading enabled).
   ```bash
   uvicorn app.main:app --reload
   ```

### Frontend (Vue 3/Vite)

1. **Install Dependencies:**
   Navigate to the `frontend/` directory and install the Node packages.
   ```bash
   cd frontend
   npm install
   ```

2. **Run the Vite Dev Server:**
   Start the Vite development server (hot module replacement enabled).
   ```bash
   npm run dev
   ```
   By default, the Vite config proxies `/api` requests to `http://localhost:8000` (your local FastAPI server).

## 3. Database Schema Changes (Alembic)

If you modify any SQLAlchemy models in `backend/app/models/`, you must generate a new Alembic migration script.

1. Generate the revision:
   ```bash
   alembic revision --autogenerate -m "Add new column to endpoints"
   ```
2. Review the generated script in `backend/migrations/versions/` to ensure it accurately reflects your intent (especially important when dealing with TimescaleDB continuous aggregates).
3. Apply the migration:
   ```bash
   alembic upgrade head
   ```

## 4. Coding Standards

- **Python:** Follow PEP-8. Utilize `black` for formatting and `flake8` for linting. Ensure all FastAPI routes use Pydantic schemas for request/response validation.
- **Vue 3:** Use the `<script setup>` Composition API syntax. Styling should be placed in scoped CSS or rely on PrimeVue component props where possible.

## 5. Submitting Changes
When your feature is complete:
1. Ensure the frontend builds successfully (`npm run build`).
2. Run backend tests (via `pytest`, if configured).
3. Submit a Pull Request targeting the `main` branch.
