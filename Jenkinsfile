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
            description: 'Enable manual Production deployment'
        )

        string(
            name: 'PRODUCTION_VERSION',
            defaultValue: '',
            description: 'Git tag: v1, v1.0.0, v4'
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
                echo 'Starting Django Single Pipeline CI/CD'
                echo "Repository: ${env.GIT_REPOSITORY}"
                echo "Branch: ${env.GIT_BRANCH}"
                echo "Staging: ${env.STAGING_HOST}"
                echo "Production: ${env.PRODUCTION_HOST}"
                echo "Production deployment: ${params.DEPLOY_PRODUCTION}"
            }
        }

        stage('Checkout Code') {
            steps {
                echo 'Checking out main branch...'

                checkout scm

                sh '''
                    set -eu

                    git fetch --force --tags origin

                    echo "Current branch:"
                    git branch --show-current

                    echo "Current commit:"
                    git rev-parse HEAD

                    git log -1 --oneline
                '''
            }
        }

        // ==========================================
        // STAGING ARTIFACT
        // ==========================================

        stage('Create Staging Artifact') {
            when {
                expression {
                    return !params.DEPLOY_PRODUCTION
                }
            }

            steps {
                sh '''
                    set -eu

                    rm -f "$STAGING_ARTIFACT"

                    TMP_DIR=$(mktemp -d)
                    trap 'rm -rf "$TMP_DIR"' EXIT

                    git archive --format=tar HEAD |
                    tar \
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
                        --exclude='*.pyo' \
                        -xf - -C "$TMP_DIR"

                    tar -czf "$STAGING_ARTIFACT" -C "$TMP_DIR" .

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

        // ==========================================
        // DEPLOY STAGING
        // ==========================================

        stage('Deploy Staging') {
            when {
                expression {
                    return !params.DEPLOY_PRODUCTION
                }
            }

            steps {
                sshagent(credentials: [env.STAGING_CREDENTIAL]) {

                    sh '''
                        set -eu

                        echo "Uploading to Staging..."

                        scp -o StrictHostKeyChecking=accept-new \
                            "$STAGING_ARTIFACT" \
                            "$DEPLOY_USER@$STAGING_HOST:/tmp/$STAGING_ARTIFACT"

                        ssh -o StrictHostKeyChecking=accept-new \
                            "$DEPLOY_USER@$STAGING_HOST" \
                            "APP_DIR='$APP_DIR' bash -s" <<'REMOTE'

set -Eeuo pipefail

APP_DIR="${APP_DIR:-/var/www/myproject}"
RELEASE_FILE="/tmp/release-staging.tar.gz"

echo "Deploying Staging..."

if [ ! -d "$APP_DIR" ] || [ ! -w "$APP_DIR" ]; then
    echo "ERROR: deploy user cannot write to $APP_DIR"
    exit 1
fi

cd "$APP_DIR"

tar \
    --no-same-owner \
    --no-same-permissions \
    --no-overwrite-dir \
    -xzf "$RELEASE_FILE" \
    -C "$APP_DIR"

rm -f "$RELEASE_FILE"

# Create virtual environment if missing
if [ ! -x venv/bin/python ]; then
    python3 -m venv venv
fi

# Install Python dependencies
venv/bin/python -m pip install -r requirements.txt

# Load existing project .env
if [ ! -f "$APP_DIR/.env" ] || [ ! -r "$APP_DIR/.env" ]; then
    echo "ERROR: Staging .env file missing or unreadable."
    exit 1
fi

set -a
source "$APP_DIR/.env"
set +a

# Collect static files
venv/bin/python manage.py collectstatic --noinput

echo "Database migration skipped."

# Restart Gunicorn
sudo -n /usr/bin/systemctl restart gunicorn
sudo -n /usr/bin/systemctl is-active --quiet gunicorn

echo "STAGING DEPLOYMENT SUCCESSFUL"

REMOTE
                    '''
                }
            }
        }

        // ==========================================
        // PRODUCTION VERSION VALIDATION
        // ==========================================

        stage('Validate Production Version') {
            when {
                expression {
                    return params.DEPLOY_PRODUCTION
                }
            }

            steps {
                script {

                    def version = (params.PRODUCTION_VERSION ?: '').trim()

                    if (!version) {
                        error('Production version is required. Example: v1.0.0')
                    }

                    if (!(version ==~ /^v[0-9]+(\\.[0-9]+){0,2}([.-][A-Za-z0-9.-]+)?$/)) {
                        error("Invalid version: ${version}")
                    }

                    withEnv(["RELEASE_VERSION=${version}"]) {

                        sh '''
                            set -eu

                            git fetch --force --tags origin

                            git rev-parse --verify \
                                "refs/tags/${RELEASE_VERSION}^{commit}" \
                                >/dev/null

                            echo "Git tag $RELEASE_VERSION exists."

                            rm -f "$PRODUCTION_ARTIFACT"

                            TMP_DIR=$(mktemp -d)
                            trap 'rm -rf "$TMP_DIR"' EXIT

                            git archive --format=tar \
                                "refs/tags/${RELEASE_VERSION}" |
                            tar \
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
                                --exclude='*.pyo' \
                                -xf - -C "$TMP_DIR"

                            tar -czf "$PRODUCTION_ARTIFACT" \
                                -C "$TMP_DIR" .

                            ls -lh "$PRODUCTION_ARTIFACT"
                        '''
                    }

                    echo "Production version validated: ${version}"
                }

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

                    def approvedBy = input(
                        message: """
PRODUCTION DEPLOYMENT APPROVAL

Application: Django
Server: ${env.PRODUCTION_HOST}
Version: ${params.PRODUCTION_VERSION}

Approve Production deployment?
""",
                        ok: 'Approve Production',
                        submitterParameter: 'APPROVED_BY'
                    )

                    echo "Approved by: ${approvedBy}"
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

                unstash 'production-release'

                sshagent(credentials: [env.PRODUCTION_CREDENTIAL]) {

                    sh '''
                        set -eu

                        echo "Uploading Production artifact..."

                        scp -o StrictHostKeyChecking=accept-new \
                            "$PRODUCTION_ARTIFACT" \
                            "$DEPLOY_USER@$PRODUCTION_HOST:/tmp/$PRODUCTION_ARTIFACT"

                        ssh -o StrictHostKeyChecking=accept-new \
                            "$DEPLOY_USER@$PRODUCTION_HOST" \
                            "APP_DIR='$APP_DIR' bash -s" <<'REMOTE'

set -Eeuo pipefail

APP_DIR="${APP_DIR:-/var/www/myproject}"
RELEASE_FILE="/tmp/release-production.tar.gz"

echo "Deploying Production..."

if [ ! -d "$APP_DIR" ] || [ ! -w "$APP_DIR" ]; then
    echo "ERROR: deploy user cannot write to $APP_DIR"
    echo "Fix directory ownership/permissions on Production server."
    exit 1
fi

cd "$APP_DIR"

tar \
    --no-same-owner \
    --no-same-permissions \
    --no-overwrite-dir \
    -xzf "$RELEASE_FILE" \
    -C "$APP_DIR"

rm -f "$RELEASE_FILE"

# Create virtual environment if missing
if [ ! -x venv/bin/python ]; then
    python3 -m venv venv
fi

# Install Python dependencies
venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install -r requirements.txt

# Load existing project .env
if [ ! -f "$APP_DIR/.env" ] || [ ! -r "$APP_DIR/.env" ]; then
    echo "ERROR: Production .env file missing or unreadable."
    exit 1
fi

set -a
source "$APP_DIR/.env"
set +a

# Collect static files
venv/bin/python manage.py collectstatic --noinput

echo "Database migration skipped."

# Restart Gunicorn
sudo -n /usr/bin/systemctl restart gunicorn
sudo -n /usr/bin/systemctl is-active --quiet gunicorn

echo "PRODUCTION DEPLOYMENT SUCCESSFUL"

REMOTE
                    '''
                }
            }
        }
    }

    // ==========================================
    // POST BUILD ACTIONS
    // ==========================================

    post {

        success {
            echo 'JENKINS PIPELINE SUCCESSFUL'
        }

        failure {
            echo 'JENKINS PIPELINE FAILED'
            echo 'Please check Console Output.'
        }

        aborted {
            echo 'JENKINS PIPELINE ABORTED'
        }

        always {
            echo "Build Number: ${env.BUILD_NUMBER}"
            echo 'Pipeline execution completed.'
        }
    }
}