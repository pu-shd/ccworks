# Versioned Playwright environment for OS dependencies; browser binaries for
# the library pinned in requirements.lock are installed below.
FROM mcr.microsoft.com/playwright/python:v1.62.0-jammy

WORKDIR /app
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"
# The Playwright image includes virtualenv, while its distro Python omits
# ensurepip (required by `python -m venv`). Use the bundled seed packages.
RUN python -m virtualenv --no-download "$VIRTUAL_ENV"

# Install the ccworks package (and its declared dependencies) editable so
# the source tree in /app is directly runnable as a module.
COPY pyproject.toml README.md LICENSE.md requirements.lock requirements-dev.lock ./
COPY src/ ./src/
COPY tests/ ./tests/
COPY .github/workflows/ci.yml ./.github/workflows/ci.yml
COPY release/ ./release/
COPY plugins/ ./plugins/
COPY .agents/plugins/marketplace.json ./.agents/plugins/marketplace.json
COPY .pi/extensions/concur.ts ./.pi/extensions/concur.ts
RUN pip install --no-cache-dir -r requirements-dev.lock -e .

# Install Chromium matching the version pinned by requirements.lock. OS-level
# dependencies come from the versioned Playwright base image.
RUN playwright install chromium

# Skip the interactive chromium bootstrap in the container — the base image
# already ships Playwright browsers.
ENV CCWORKS_SKIP_BROWSER_BOOTSTRAP=1

# Default: run the mock unit tests
CMD ["python", "-m", "unittest", "discover", "-v", "-s", "tests"]
