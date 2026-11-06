
pipeline {
    agent any

    options {
        timestamps()
        disableConcurrentBuilds()
        skipDefaultCheckout(true)
        buildDiscarder(
            logRotator(
                numToKeepStr: '20',
                artifactNumToKeepStr: '10'
            )
        )
    }

    triggers {
        githubPush()
    }

    parameters {
        booleanParam(
            name: 'DEPLOY_PRODUCTION',
            defaultValue: false,
            description: 'Enable this option to deploy Production'
        )

        string(
            name: 'PRODUCTION_VERSION',
            defaultValue: '',
            description: 'Git tag to deploy. Example: v1.0.0'
        )
    }

    environment {
        GIT_REPOSITORY = 'https://github.com/ikbal-sebpo/python.git'
        GIT_BRANCH = 'main'

        STAGING_HOST = '192.168.119.128'
        PRODUCTION_HOST = '192.168.119.130'

        DEPLOY_USER = 'deploy'
        APP_DIR = '/var/www/myproject'

        STAGING_CREDENTIAL = 'staging-ssh-key'
        PRODUCTION_CREDENTIAL = 'staging-ssh-key'

        STAGING_ARTIFACT = 'release-staging.tar.gz'
        PRODUCTION_ARTIFACT = 'release-production.tar.gz'
    }

    stages {

        stage('Start') {
            steps {
                echo '========================================'
                echo '       DJANGO JENKINS CI/CD'
                echo '========================================'
                echo "Repository: ${GIT_REPOSITORY}"
                echo "Branch: ${GIT_BRANCH}"
                echo "Staging: ${STAGING_HOST}"
                echo "Production: ${PRODUCTION_HOST}"
                echo "Production Deployment: ${params.DEPLOY_PRODUCTION}"
            }
        }

        stage('Checkout Code') {
            steps {
                checkout scm

                sh '''
                    set -eu

                    git fetch --force --tags origin

                    echo "Current commit:"
                    git rev-parse HEAD

                    echo "Current branch:"
                    git branch --show-current

                    echo "Latest commit:"
                    git log -1 --oneline
                '''
            }
        }

        // ========================================
        // STAGING ARTIFACT
        // ========================================

        stage('Create Staging Artifact') {
            when {
                expression {
                    return !params.DEPLOY_PRODUCTION
                }
            }

            steps {
                sh '''
                    set -eu

                    echo "Creating Staging artifact..."

                    TMP_DIR="$(mktemp -d)"

                    cleanup() {
                        rm -rf "$TMP_DIR"
                    }
                    trap cleanup EXIT

                    git archive --format=tar HEAD | tar -xf - -C "$TMP_DIR"

                    rm -rf \
                        "$TMP_DIR/.env" \
                        "$TMP_DIR/venv" \
                        "$TMP_DIR/.ci-venv" \
                        "$TMP_DIR/staticfiles" \
                        "$TMP_DIR/media" \
                        "$TMP_DIR/.git"

                    find "$TMP_DIR" -type d -name "__pycache__" \
                        -prune -exec rm -rf {} +

                    find "$TMP_DIR" -type f \\( \
                        -name "*.pyc" -o \
                        -name "*.pyo" \
                    \\) -delete

                    tar -czf "$STAGING_ARTIFACT" -C "$TMP_DIR" .

                    echo "Staging artifact created:"
                    ls -lh "$STAGING_ARTIFACT"
                '''

                archiveArtifacts(
                    artifacts: 'release-staging.tar.gz',
                    fingerprint: true
                )

                stash(
                    name: 'staging-release',
                    includes: 'release-staging.tar.gz'
                )
            }
        }

        // ========================================
        // STAGING DEPLOYMENT
        // ========================================

        stage('Deploy Staging') {
            when {
                expression {
                    return !params.DEPLOY_PRODUCTION
                }
            }

            steps {
                echo 'Deploying to Staging Server...'

                unstash 'staging-release'

                sshagent(credentials: [env.STAGING_CREDENTIAL]) {
                    sh '''
                        set -eu

                        echo "Uploading Staging artifact..."

                        scp \
                            -o StrictHostKeyChecking=accept-new \
                            "$STAGING_ARTIFACT" \
                            "$DEPLOY_USER@$STAGING_HOST:/tmp/$STAGING_ARTIFACT"

                        ssh \
                            -o StrictHostKeyChecking=accept-new \
                            "$DEPLOY_USER@$STAGING_HOST" \
                            "APP_DIR='$APP_DIR' RELEASE_FILE='$STAGING_ARTIFACT' bash -s" <<'REMOTE'

set -eu

echo "========================================"
echo "       STAGING DEPLOYMENT"
echo "========================================"

echo "Application directory: $APP_DIR"

if [ ! -d "$APP_DIR" ]; then
    echo "ERROR: Application directory not found."
    exit 1
fi

cd "$APP_DIR"

# Preserve existing .env file.
if [ ! -f "$APP_DIR/.env" ] || [ ! -r "$APP_DIR/.env" ]; then
    echo "ERROR: .env file missing or unreadable."
    exit 1
fi

echo "Existing .env file found."

# Extract application code.
tar -xzf "/tmp/$RELEASE_FILE" -C "$APP_DIR"

rm -f "/tmp/$RELEASE_FILE"

# Create virtual environment if missing.
if [ ! -x "$APP_DIR/venv/bin/python" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv "$APP_DIR/venv"
fi

# Install dependencies.
echo "Installing Python dependencies..."

"$APP_DIR/venv/bin/python" -m pip install --upgrade pip

"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# Collect static files.
echo "Collecting static files..."

"$APP_DIR/venv/bin/python" "$APP_DIR/manage.py" collectstatic --noinput

# ========================================
# STATIC FILE PERMISSIONS
# ========================================

echo "Setting Static Files Permissions..."

sudo -n /usr/bin/chmod 755 /var/www
sudo -n /usr/bin/chmod 755 /var/www/myproject
sudo -n /usr/bin/chmod -R a+rX /var/www/myproject/staticfiles

if [ -d "$APP_DIR/staticfiles" ]; then
    sudo -n /usr/bin/chmod -R a+rX "$APP_DIR/staticfiles"
else
    echo "ERROR: staticfiles directory not found."
    exit 1
fi

echo "Static files permissions updated."

# No database migration.
echo "Database migration skipped."

# Restart Gunicorn.
echo "Restarting Gunicorn..."

sudo -n /usr/bin/systemctl restart gunicorn

sleep 3

if ! sudo -n /usr/bin/systemctl is-active --quiet gunicorn; then
    echo "ERROR: Gunicorn is not active."
    sudo -n /usr/bin/systemctl status gunicorn --no-pager || true
    exit 1
fi

echo "Gunicorn is running."

echo "========================================"
echo "       STAGING DEPLOY SUCCESS"
echo "========================================"

REMOTE
                    '''
                }
            }
        }

        // ========================================
        // PRODUCTION VERSION VALIDATION
        // ========================================

        stage('Validate Production Version') {
            when {
                expression {
                    return params.DEPLOY_PRODUCTION
                }
            }

            steps {
                script {
                    env.RELEASE_VERSION = params.PRODUCTION_VERSION.trim()

                    if (!env.RELEASE_VERSION) {
                        error('PRODUCTION_VERSION is required.')
                    }

                    if (!(env.RELEASE_VERSION ==~ /^v[0-9]+(\.[0-9]+){0,2}([.-][A-Za-z0-9.-]+)?$/)) {
                        error(
                            "Invalid production version: ${env.RELEASE_VERSION}. " +
                            "Example: v1, v1.0.0 or v1.2.3"
                        )
                    }
                }

                sh '''
                    set -eu

                    echo "Fetching Git tags..."

                    git fetch --force --tags origin

                    echo "Validating tag: $RELEASE_VERSION"

                    git rev-parse --verify \
                        "refs/tags/${RELEASE_VERSION}^{commit}" >/dev/null

                    echo "Production tag is valid."

                    git show -s \
                        --format="Tag: %D%nCommit: %H%nDate: %ci%nMessage: %s" \
                        "$RELEASE_VERSION"
                '''
            }
        }

        // ========================================
        // PRODUCTION ARTIFACT
        // ========================================

        stage('Create Production Artifact') {
            when {
                expression {
                    return params.DEPLOY_PRODUCTION
                }
            }

            steps {
                sh '''
                    set -eu

                    echo "Creating Production artifact..."

                    TMP_DIR="$(mktemp -d)"

                    cleanup() {
                        rm -rf "$TMP_DIR"
                    }
                    trap cleanup EXIT

                    git archive --format=tar "$RELEASE_VERSION" \
                        | tar -xf - -C "$TMP_DIR"

                    rm -rf \
                        "$TMP_DIR/.env" \
                        "$TMP_DIR/venv" \
                        "$TMP_DIR/.ci-venv" \
                        "$TMP_DIR/staticfiles" \
                        "$TMP_DIR/media" \
                        "$TMP_DIR/.git"

                    find "$TMP_DIR" -type d -name "__pycache__" \
                        -prune -exec rm -rf {} +

                    find "$TMP_DIR" -type f \\( \
                        -name "*.pyc" -o \
                        -name "*.pyo" \
                    \\) -delete

                    tar -czf "$PRODUCTION_ARTIFACT" -C "$TMP_DIR" .

                    echo "Production artifact created:"
                    ls -lh "$PRODUCTION_ARTIFACT"
                '''

                archiveArtifacts(
                    artifacts: 'release-production.tar.gz',
                    fingerprint: true
                )

                stash(
                    name: 'production-release',
                    includes: 'release-production.tar.gz'
                )
            }
        }

        // ========================================
        // PRODUCTION APPROVAL
        // ========================================

        stage('Production Approval') {
            when {
                expression {
                    return params.DEPLOY_PRODUCTION
                }
            }

            steps {
                script {
                    def approval = input(
                        message: """
Production Deployment Approval

Application: Django
Version: ${env.RELEASE_VERSION}
Server: ${env.PRODUCTION_HOST}
Directory: ${env.APP_DIR}

Do you approve this deployment?
""",
                        ok: 'Approve Production Deployment',
                        submitterParameter: 'APPROVED_BY'
                    )

                    echo "Production approved by: ${approval}"
                }
            }
        }

        // ========================================
        // PRODUCTION DEPLOYMENT
        // ========================================

        stage('Deploy Production') {
            when {
                expression {
                    return params.DEPLOY_PRODUCTION
                }
            }

            steps {
                echo "Deploying Production version: ${env.RELEASE_VERSION}"

                unstash 'production-release'

                sshagent(credentials: [env.PRODUCTION_CREDENTIAL]) {
                    sh '''
                        set -eu

                        echo "Uploading Production artifact..."

                        scp \
                            -o StrictHostKeyChecking=accept-new \
                            "$PRODUCTION_ARTIFACT" \
                            "$DEPLOY_USER@$PRODUCTION_HOST:/tmp/$PRODUCTION_ARTIFACT"

                        ssh \
                            -o StrictHostKeyChecking=accept-new \
                            "$DEPLOY_USER@$PRODUCTION_HOST" \
                            "APP_DIR='$APP_DIR' RELEASE_FILE='$PRODUCTION_ARTIFACT' bash -s" <<'REMOTE'

set -eu

echo "========================================"
echo "      PRODUCTION DEPLOYMENT"
echo "========================================"

echo "Application directory: $APP_DIR"

if [ ! -d "$APP_DIR" ]; then
    echo "ERROR: Application directory not found."
    exit 1
fi

cd "$APP_DIR"

# Preserve existing .env file.
if [ ! -f "$APP_DIR/.env" ] || [ ! -r "$APP_DIR/.env" ]; then
    echo "ERROR: .env file missing or unreadable."
    exit 1
fi

echo "Existing .env file found."

# Extract application code.
tar -xzf "/tmp/$RELEASE_FILE" -C "$APP_DIR"

rm -f "/tmp/$RELEASE_FILE"

# Create virtual environment if missing.
if [ ! -x "$APP_DIR/venv/bin/python" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv "$APP_DIR/venv"
fi

# Install dependencies.
echo "Installing Python dependencies..."

"$APP_DIR/venv/bin/python" -m pip install --upgrade pip

"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# Collect static files.
echo "Collecting static files..."

"$APP_DIR/venv/bin/python" "$APP_DIR/manage.py" collectstatic --noinput

# ========================================
# STATIC FILE PERMISSIONS
# ========================================

echo "Setting Static Files Permissions..."

sudo -n /usr/bin/chmod 755 /var/www

sudo -n /usr/bin/chmod 755 /var/www/myproject

if [ -d "$APP_DIR/staticfiles" ]; then
    sudo -n /usr/bin/chmod -R a+rX "$APP_DIR/staticfiles"
else
    echo "ERROR: staticfiles directory not found."
    exit 1
fi

echo "Static files permissions updated."

# No database migration.
echo "Database migration skipped."

# Restart Gunicorn.
echo "Restarting Gunicorn..."

sudo -n /usr/bin/systemctl restart gunicorn

sleep 3

if ! sudo -n /usr/bin/systemctl is-active --quiet gunicorn; then
    echo "ERROR: Gunicorn is not active."
    sudo -n /usr/bin/systemctl status gunicorn --no-pager || true
    exit 1
fi

echo "Gunicorn is running."

echo "========================================"
echo "      PRODUCTION DEPLOY SUCCESS"
echo "========================================"

REMOTE
                    '''
                }
            }
        }
    }

    // ========================================
    // POST ACTIONS
    // ========================================

    post {
        success {
            echo '========================================'
            echo '       PIPELINE SUCCESS'
            echo '========================================'

            script {
                if (params.DEPLOY_PRODUCTION) {
                    echo "Production deployed: ${env.RELEASE_VERSION}"
                } else {
                    echo 'Staging deployment completed.'
                }
            }
        }

        failure {
            echo '========================================'
            echo '       PIPELINE FAILED'
            echo '========================================'
            echo 'Please check Jenkins console output.'
        }

        aborted {
            echo 'Pipeline was aborted.'
        }

        always {
            echo 'Jenkins CI/CD pipeline finished.'
        }
    }
}
