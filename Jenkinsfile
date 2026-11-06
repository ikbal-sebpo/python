pipeline {

    agent any

    options {
        timestamps()
        disableConcurrentBuilds()
        skipDefaultCheckout(true)
        buildDiscarder(logRotator(
            numToKeepStr: '20',
            artifactNumToKeepStr: '10'
        ))
    }

    triggers {
        githubPush()
    }

    parameters {
        booleanParam(
            name: 'DEPLOY_PRODUCTION',
            defaultValue: false,
            description: 'Enable Production deployment'
        )

        string(
            name: 'PRODUCTION_VERSION',
            defaultValue: '',
            description: 'Git tag to deploy. Example: v1, v1.0.0, v2'
        )
    }

    environment {
        // GitHub
        GIT_REPOSITORY = 'https://github.com/ikbal-sebpo/python.git'
        GIT_BRANCH = 'main'

        // Servers
        STAGING_HOST = '192.168.119.128'
        PRODUCTION_HOST = '192.168.119.130'

        // SSH
        DEPLOY_USER = 'deploy'
        STAGING_CREDENTIAL = 'staging-ssh-key'
        PRODUCTION_CREDENTIAL = 'staging-ssh-key'

        // Django application
        APP_DIR = '/var/www/myproject'

        // Environment files on target servers
        STAGING_ENV_FILE = '/etc/myproject.env'
        PRODUCTION_ENV_FILE = '/etc/myproject.env'

        // Artifact names
        STAGING_ARTIFACT = 'release-staging.tar.gz'
        PRODUCTION_ARTIFACT = 'release-production.tar.gz'
    }

    stages {

        stage('Start') {
            steps {
                echo '======================================'
                echo 'Starting Django CI/CD Pipeline'
                echo '======================================'
                echo "Staging Server: ${STAGING_HOST}"
                echo "Production Server: ${PRODUCTION_HOST}"
                echo "Application: ${APP_DIR}"
                echo "Branch: ${GIT_BRANCH}"
                echo "Production Deployment: ${params.DEPLOY_PRODUCTION}"
            }
        }

        // ==========================================
        // CHECKOUT CODE
        // ==========================================

        stage('Checkout Code') {
            steps {
                echo 'Checking out GitHub source code...'

                checkout([
                    $class: 'GitSCM',
                    branches: [[name: "*/${GIT_BRANCH}"]],
                    userRemoteConfigs: [[
                        url: "${GIT_REPOSITORY}"
                    ]]
                ])

                sh '''
                    echo "Current Git commit:"
                    git log -1 --oneline
                '''
            }
        }

        // ==========================================
        // CREATE STAGING ARTIFACT
        // ==========================================

        stage('Create Staging Artifact') {
            steps {
                echo 'Creating Staging release artifact...'

                sh '''
                    set -e

                    rm -f "$STAGING_ARTIFACT"

                    TMP_DIR=$(mktemp -d)

                    trap 'rm -rf "$TMP_DIR"' EXIT

                    git archive HEAD | tar -x -C "$TMP_DIR" \
                        --exclude='.env' \
                        --exclude='*/.env' \
                        --exclude='venv' \
                        --exclude='*/venv' \
                        --exclude='.ci-venv' \
                        --exclude='staticfiles' \
                        --exclude='*/staticfiles' \
                        --exclude='media' \
                        --exclude='*/media' \
                        --exclude='db.sqlite3' \
                        --exclude='*/db.sqlite3' \
                        --exclude='__pycache__' \
                        --exclude='*/__pycache__' \
                        --exclude='*.pyc' \
                        --exclude='*.pyo'

                    tar -czf "$STAGING_ARTIFACT" -C "$TMP_DIR" .

                    echo "Staging artifact created:"
                    ls -lh "$STAGING_ARTIFACT"
                '''
            }
        }

        // ==========================================
        // DEPLOY STAGING
        // ==========================================

        stage('Deploy Staging') {
            steps {
                echo 'Deploying application to Staging...'

                sshagent(credentials: [env.STAGING_CREDENTIAL]) {

                    sh '''
                        set -e

                        echo "Uploading artifact to Staging..."

                        scp -o StrictHostKeyChecking=accept-new \
                            "$STAGING_ARTIFACT" \
                            "$DEPLOY_USER@$STAGING_HOST:/tmp/$STAGING_ARTIFACT"

                        echo "Starting Staging deployment..."

                        ssh -o StrictHostKeyChecking=accept-new \
                            "$DEPLOY_USER@$STAGING_HOST" \
                            "APP_DIR='$APP_DIR' ENV_FILE='$STAGING_ENV_FILE' bash -s" <<'REMOTE_SCRIPT'

set -Eeuo pipefail

echo "======================================"
echo "STAGING DEPLOYMENT"
echo "======================================"

APP_DIR="${APP_DIR:-/var/www/myproject}"
ENV_FILE="${ENV_FILE:-/etc/myproject.env}"
RELEASE_FILE="/tmp/release-staging.tar.gz"

echo "Application directory: $APP_DIR"

# Create application directory
mkdir -p "$APP_DIR"

# Extract application
echo "Extracting release..."

tar -xzf "$RELEASE_FILE" -C "$APP_DIR"

rm -f "$RELEASE_FILE"

cd "$APP_DIR"

# Create virtual environment if missing
if [ ! -d "venv" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install dependencies
echo "Installing Python dependencies..."

python -m pip install --upgrade pip
pip install -r requirements.txt

# Load environment variables if available
if [ -f "$ENV_FILE" ]; then

    if [ ! -r "$ENV_FILE" ]; then
        echo "ERROR: Environment file is not readable: $ENV_FILE"
        exit 1
    fi

    echo "Loading Staging environment..."

    set -a
    source "$ENV_FILE"
    set +a

else
    echo "Environment file not found: $ENV_FILE"
    echo "Continuing with existing application environment configuration..."
fi

# Django static files
echo "Collecting static files..."

python manage.py collectstatic --noinput

# Restart Gunicorn
echo "Restarting Gunicorn..."

sudo systemctl restart gunicorn

# Verify service
sudo systemctl is-active --quiet gunicorn

echo "======================================"
echo "STAGING DEPLOYMENT SUCCESSFUL"
echo "======================================"

REMOTE_SCRIPT

                    '''

                }
            }
        }

        // ==========================================
        // VALIDATE PRODUCTION VERSION
        // ==========================================

        stage('Validate Production Version') {

            when {
                expression {
                    return params.DEPLOY_PRODUCTION
                }
            }

            steps {
                script {

                    def version = params.PRODUCTION_VERSION.trim()

                    if (!version) {
                        error('PRODUCTION_VERSION is required for Production deployment.')
                    }

                    if (!(version ==~ /^v[0-9]+(\\.[0-9]+){0,2}([.-][A-Za-z0-9.-]+)?$/)) {
                        error(
                            "Invalid version: ${version}. " +
                            "Examples: v1, v1.0, v1.0.0, v2"
                        )
                    }

                    echo "Validating Production version: ${version}"

                    sh """
                        set -e

                        git fetch --force --tags origin

                        git rev-parse --verify \
                            'refs/tags/${version}^{commit}' >/dev/null

                        echo "Git tag ${version} exists."
                    """

                    // Create production artifact from the selected tag
                    sh """
                        set -e

                        rm -f "$PRODUCTION_ARTIFACT"

                        TMP_DIR=\$(mktemp -d)

                        trap 'rm -rf "\\$TMP_DIR"' EXIT

                        git archive "refs/tags/${version}" | tar -x -C "\\$TMP_DIR" \
                            --exclude='.env' \
                            --exclude='*/.env' \
                            --exclude='venv' \
                            --exclude='*/venv' \
                            --exclude='.ci-venv' \
                            --exclude='staticfiles' \
                            --exclude='*/staticfiles' \
                            --exclude='media' \
                            --exclude='*/media' \
                            --exclude='db.sqlite3' \
                            --exclude='*/db.sqlite3' \
                            --exclude='__pycache__' \
                            --exclude='*/__pycache__' \
                            --exclude='*.pyc' \
                            --exclude='*.pyo'

                        tar -czf "$PRODUCTION_ARTIFACT" -C "\\$TMP_DIR" .

                        echo "Production artifact created:"
                        ls -lh "$PRODUCTION_ARTIFACT"
                    """

                    echo "Production artifact ready for version ${version}"
                }
            }
        }

        // ==========================================
        // PRODUCTION APPROVAL
        // ==========================================

        stage('Production Approval') {

            when {
                expression {
                    return params.DEPLOY_PRODUCTION
                }
            }

            steps {

                script {

                    input(
                        message: """
                            Production Deployment Approval

                            Application: Django
                            Server: ${PRODUCTION_HOST}
                            Version: ${params.PRODUCTION_VERSION}

                            Do you want to deploy this version to Production?
                        """,
                        ok: 'Approve Production Deployment'
                    )

                }
            }
        }

        // ==========================================
        // DEPLOY PRODUCTION
        // ==========================================

        stage('Deploy Production') {

            when {
                expression {
                    return params.DEPLOY_PRODUCTION
                }
            }

            steps {

                echo "Deploying version ${params.PRODUCTION_VERSION} to Production..."

                sshagent(credentials: [env.PRODUCTION_CREDENTIAL]) {

                    sh '''
                        set -e

                        echo "Uploading Production artifact..."

                        scp -o StrictHostKeyChecking=accept-new \
                            "$PRODUCTION_ARTIFACT" \
                            "$DEPLOY_USER@$PRODUCTION_HOST:/tmp/$PRODUCTION_ARTIFACT"

                        echo "Starting Production deployment..."

                        ssh -o StrictHostKeyChecking=accept-new \
                            "$DEPLOY_USER@$PRODUCTION_HOST" \
                            "APP_DIR='$APP_DIR' ENV_FILE='$PRODUCTION_ENV_FILE' bash -s" <<'REMOTE_SCRIPT'

set -Eeuo pipefail

echo "======================================"
echo "PRODUCTION DEPLOYMENT"
echo "======================================"

APP_DIR="${APP_DIR:-/var/www/myproject}"
ENV_FILE="${ENV_FILE:-/etc/myproject.env}"
RELEASE_FILE="/tmp/release-production.tar.gz"

# Create application directory before using it
sudo mkdir -p "$APP_DIR"

# Ensure deploy user can manage application files
sudo chown -R "$(whoami):$(id -gn)" "$APP_DIR"

cd "$APP_DIR"

# Extract release
echo "Extracting Production release..."

tar -xzf "$RELEASE_FILE" -C "$APP_DIR"

rm -f "$RELEASE_FILE"

# Create virtual environment if missing
if [ ! -d "venv" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install dependencies
echo "Installing Python dependencies..."

python -m pip install --upgrade pip
pip install -r requirements.txt

# Validate environment file
echo "Loading Production Environment..."

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: Environment file not found: $ENV_FILE"
    exit 1
fi

if [ ! -r "$ENV_FILE" ]; then
    echo "ERROR: Environment file is not readable: $ENV_FILE"
    exit 1
fi

# Validate shell syntax
if ! bash -n "$ENV_FILE"; then
    echo "ERROR: Invalid syntax in $ENV_FILE"
    exit 1
fi

# Load environment variables
set -a
source "$ENV_FILE"
set +a

echo "Production environment loaded successfully."

# Django static files
echo "Collecting static files..."

python manage.py collectstatic --noinput

# No database migration
echo "Database migration skipped as requested."

# Restart Gunicorn
echo "Restarting Production Gunicorn..."

sudo systemctl restart gunicorn

# Verify Gunicorn
if ! sudo systemctl is-active --quiet gunicorn; then
    echo "ERROR: Gunicorn service is not active."
    sudo systemctl status gunicorn --no-pager || true
    exit 1
fi

echo "======================================"
echo "PRODUCTION DEPLOYMENT SUCCESSFUL"
echo "======================================"

REMOTE_SCRIPT

                    '''

                }
            }
        }
    }

    // ==========================================
    // POST ACTIONS
    // ==========================================

    post {

        success {
            echo '======================================'
            echo 'JENKINS PIPELINE SUCCESSFUL'
            echo '======================================'
        }

        failure {
            echo '======================================'
            echo 'JENKINS PIPELINE FAILED'
            echo 'Please check the Console Output.'
            echo '======================================'
        }

        aborted {
            echo 'Jenkins pipeline was aborted.'
        }

        always {
            echo 'Pipeline execution completed.'
        }
    }
}