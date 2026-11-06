
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

        string(
            name: 'PRODUCTION_VERSION',
            defaultValue: '',
            description: 'Git tag to deploy. Example: v1.0.0'
        )

        booleanParam(
            name: 'DEPLOY_PRODUCTION',
            defaultValue: false,
            description: 'Enable manual Production deployment'
        )
    }

    environment {

        // Server Configuration
        STAGING_HOST = '192.168.119.128'
        PRODUCTION_HOST = '192.168.119.130'

        DEPLOY_USER = 'deploy'

        APP_DIR = '/var/www/myproject'

        // Jenkins SSH Credentials IDs
        STAGING_CREDENTIAL = 'staging-ssh-key'
        PRODUCTION_CREDENTIAL = 'staging-ssh-key'

        // GitHub Repository
        GIT_REPOSITORY = 'https://github.com/ikbal-sebpo/python.git'
        GIT_BRANCH = 'main'
    }

    stages {

        // ==========================================
        // 1. START PIPELINE
        // ==========================================

        stage('Start') {
            steps {

                echo '===================================='
                echo 'Starting Django CI/CD Pipeline'
                echo '===================================='

                echo 'Staging: Automatic Deployment'
                echo 'Production: Manual Approval'
            }
        }


        // ==========================================
        // 2. CHECKOUT SOURCE CODE
        // ==========================================

        stage('Checkout') {
            steps {

                checkout scm

                sh '''
                    set -e

                    git fetch --force --tags origin

                    echo "Current Branch:"
                    git branch --show-current

                    echo "Current Commit:"
                    git rev-parse HEAD

                    echo "Repository:"
                    git remote get-url origin
                '''
            }
        }


        // ==========================================
        // 3. CREATE STAGING ARTIFACT
        // ==========================================

        stage('Create Staging Artifact') {
            steps {

                sh '''
                    set -e

                    echo "Creating Staging Artifact..."

                    ARCHIVE_TMP="${WORKSPACE}@tmp/release-staging.tar.gz"

                    mkdir -p "${WORKSPACE}@tmp"

                    tar -czf "$ARCHIVE_TMP" \
                        --exclude='.git' \
                        --exclude='venv' \
                        --exclude='.ci-venv' \
                        --exclude='staticfiles' \
                        --exclude='media' \
                        --exclude='.env' \
                        --exclude='__pycache__' \
                        --exclude='*.pyc' \
                        --exclude='db.sqlite3' \
                        --exclude='Jenkinsfile' \
                        --exclude='release-staging.tar.gz' \
                        --exclude='release-production.tar.gz' \
                        -C "$WORKSPACE" .

                    mv "$ARCHIVE_TMP" "$WORKSPACE/release-staging.tar.gz"

                    echo "Staging Artifact Created Successfully"
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
        // 4. DEPLOY STAGING AUTOMATICALLY
        // ==========================================

        stage('Deploy Staging') {
            steps {

                sshagent(credentials: [env.STAGING_CREDENTIAL]) {

                    sh '''
                        set -e

                        echo "===================================="
                        echo "Deploying to Staging Server"
                        echo "Server: ${STAGING_HOST}"
                        echo "===================================="

                        scp -o StrictHostKeyChecking=accept-new \
                            release-staging.tar.gz \
                            ${DEPLOY_USER}@${STAGING_HOST}:/tmp/release-staging.tar.gz

                        ssh -o StrictHostKeyChecking=accept-new \
                            ${DEPLOY_USER}@${STAGING_HOST} 'bash -s' <<'REMOTE_SCRIPT'

set -e

APP_DIR="/var/www/myproject"

cd "$APP_DIR"

echo "Extracting application files..."

tar --no-same-owner \
    --no-same-permissions \
    --no-overwrite-dir \
    -xzf /tmp/release-staging.tar.gz \
    -C "$APP_DIR"

rm -f /tmp/release-staging.tar.gz

echo "Activating virtual environment..."

test -x venv/bin/python

. venv/bin/activate

echo "Installing Python dependencies..."

pip install -r requirements.txt

echo "Collecting static files..."

python manage.py collectstatic --noinput

echo "Restarting Gunicorn..."

sudo -n /usr/bin/systemctl restart gunicorn

sudo -n /usr/bin/systemctl is-active --quiet gunicorn

echo "===================================="
echo "STAGING DEPLOYMENT SUCCESSFUL"
echo "===================================="

REMOTE_SCRIPT

                    '''
                }
            }
        }


        // ==========================================
        // 5. VALIDATE PRODUCTION VERSION
        // ==========================================

        stage('Validate Production Version') {

            when {
                expression {
                    return params.DEPLOY_PRODUCTION
                }
            }

            steps {

                script {

                    def version = params.PRODUCTION_VERSION?.trim()

                    if (!version) {
                        error(
                            'PRODUCTION_VERSION is required when Production deployment is enabled.'
                        )
                    }

                    // Accept v1, v1.0, v1.0.0 and prerelease tags
                    if (!(version ==~ /^v[0-9]+(\\.[0-9]+){0,2}([.-][A-Za-z0-9.-]+)?$/)) {

                        error(
                            "Invalid Production Version: '${version}'. Example: v1.0.0"
                        )
                    }

                    env.PRODUCTION_VERSION_CLEAN = version

                    echo "Production Version: ${version}"
                }

                sh '''
                    set -e

                    echo "===================================="
                    echo "Validating Production Git Tag"
                    echo "===================================="

                    git fetch --force --tags origin

                    git rev-parse --verify \
                        "refs/tags/${PRODUCTION_VERSION_CLEAN}^{commit}"

                    echo "Preparing Production Artifact..."

                    RELEASE_DIR=$(mktemp -d)

                    trap 'rm -rf "$RELEASE_DIR"' EXIT

                    git archive \
                        "${PRODUCTION_VERSION_CLEAN}" \
                        | tar -xf - -C "$RELEASE_DIR"

                    tar -czf release-production.tar.gz \
                        --exclude='.git' \
                        --exclude='venv' \
                        --exclude='.ci-venv' \
                        --exclude='staticfiles' \
                        --exclude='media' \
                        --exclude='.env' \
                        --exclude='__pycache__' \
                        --exclude='*.pyc' \
                        --exclude='db.sqlite3' \
                        --exclude='Jenkinsfile' \
                        -C "$RELEASE_DIR" .

                    echo "Production Artifact Created Successfully"
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


        // ==========================================
        // 6. PRODUCTION APPROVAL
        // ==========================================

        stage('Production Approval') {

            when {
                expression {
                    return params.DEPLOY_PRODUCTION
                }
            }

            steps {

                script {

                    echo 'Waiting for Production Approval...'

                    def approvedBy = input(

                        message: """
========================================

PRODUCTION DEPLOYMENT APPROVAL

Repository: ${env.GIT_REPOSITORY}

Server: ${env.PRODUCTION_HOST}

Version: ${env.PRODUCTION_VERSION_CLEAN}

Application: Django

Approve this Production Deployment?

========================================
""",

                        ok: 'Approve Production Deployment',

                        submitterParameter: 'APPROVED_BY'
                    )

                    echo "Production Approved By: ${approvedBy}"
                }
            }
        }


        // ==========================================
        // 7. DEPLOY PRODUCTION
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
                        set -e

                        echo "===================================="
                        echo "Deploying to Production Server"
                        echo "Server: ${PRODUCTION_HOST}"
                        echo "Version: ${PRODUCTION_VERSION_CLEAN}"
                        echo "===================================="

                        scp -o StrictHostKeyChecking=accept-new \
                            release-production.tar.gz \
                            ${DEPLOY_USER}@${PRODUCTION_HOST}:/tmp/myproject-release.tar.gz

                        ssh -o StrictHostKeyChecking=accept-new \
                            ${DEPLOY_USER}@${PRODUCTION_HOST} 'bash -s' <<'REMOTE_SCRIPT'

set -e

APP_DIR="/var/www/myproject"

cd "$APP_DIR"

echo "Extracting Production Release..."

tar --no-same-owner \
    --no-same-permissions \
    --no-overwrite-dir \
    -xzf /tmp/myproject-release.tar.gz \
    -C "$APP_DIR"

rm -f /tmp/myproject-release.tar.gz

echo "Checking Virtual Environment..."

if [ ! -x venv/bin/python ]; then

    python3 -m venv venv

fi

echo "Activating Virtual Environment..."

. venv/bin/activate

echo "Installing Python Dependencies..."

pip install --upgrade pip

pip install -r requirements.txt

echo "Loading Production Environment..."

test -f /etc/myproject.env

set -a
. /etc/myproject.env
set +a

echo "Collecting Static Files..."

python manage.py collectstatic --noinput

echo "Restarting Gunicorn..."

sudo -n /usr/bin/systemctl restart gunicorn

sudo -n /usr/bin/systemctl is-active --quiet gunicorn

echo "===================================="
echo "PRODUCTION DEPLOYMENT SUCCESSFUL"
echo "===================================="

REMOTE_SCRIPT

                    '''
                }
            }
        }

    }


    // ==========================================
    // 8. POST BUILD ACTIONS
    // ==========================================

    post {

        success {

            echo '''
====================================
CI/CD PIPELINE SUCCESSFUL
====================================
'''
        }

        failure {

            echo '''
====================================
CI/CD PIPELINE FAILED
Please check Jenkins Console Output.
====================================
'''
        }

        aborted {

            echo '''
====================================
CI/CD PIPELINE ABORTED
====================================
'''
        }

        always {

            echo 'Jenkins Pipeline Execution Finished.'
        }
    }
}
