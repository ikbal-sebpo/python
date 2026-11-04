pipeline {

    agent any

    options {
        timestamps()
        disableConcurrentBuilds()
    }

    triggers {
        githubPush()
    }

    parameters {

        string(
            name: 'PRODUCTION_VERSION',
            defaultValue: '',
            description: 'Git tag/version to deploy to Production. Example: v1.0.0'
        )

        booleanParam(
            name: 'DEPLOY_PRODUCTION',
            defaultValue: false,
            description: 'Enable Production deployment'
        )
    }

    environment {

        STAGING_HOST = '192.168.119.128'
        PRODUCTION_HOST = '192.168.119.130'

        DEPLOY_USER = 'deploy'

        APP_DIR = '/var/www/myproject'

        STAGING_CREDENTIAL = 'staging-ssh-key'
        PRODUCTION_CREDENTIAL = 'staging-ssh-key'
    }

    stages {

        stage('Start') {
            steps {
                echo 'Starting Django CI/CD Pipeline'
                echo 'Staging deployment will run automatically.'
            }
        }

        stage('Checkout') {
            steps {

                checkout scm

                sh '''
                    git fetch --force --tags origin

                    echo "Current Branch:"
                    git branch --show-current

                    echo "Current Commit:"
                    git rev-parse HEAD
                '''
            }
        }

        stage('Build') {
            steps {

                sh '''
                    set -e

                    if [ ! -f requirements.txt ]; then
                        echo "requirements.txt not found!"
                        exit 1
                    fi

                    python3 -m venv .ci-venv

                    .ci-venv/bin/pip install --upgrade pip

                    .ci-venv/bin/pip install -r requirements.txt
                '''
            }
        }

        stage('Test') {
            steps {

                sh '''
                    set -e

                    echo "Checking Python source compilation"

                    .ci-venv/bin/python -m compileall -q manage.py config

                    echo "Code compilation successful"
                '''
            }
        }

        stage('Create Artifact') {
            steps {

                sh '''
                    set -e

                    tar -czf release-staging.tar.gz \
                        --exclude=.git \
                        --exclude=.ci-venv \
                        --exclude=venv \
                        --exclude=staticfiles \
                        --exclude=media \
                        --exclude=.env \
                        --exclude=__pycache__ \
                        .
                '''

                archiveArtifacts artifacts: 'release-staging.tar.gz'

                stash(
                    name: 'staging-release',
                    includes: 'release-staging.tar.gz'
                )
            }
        }

        stage('Deploy Staging') {
            steps {

                unstash 'staging-release'

                sshagent(credentials: [env.STAGING_CREDENTIAL]) {

                    sh '''
                        set -e

                        echo "Deploying to Staging Server"

                        scp -o StrictHostKeyChecking=accept-new \
                            release-staging.tar.gz \
                            ${DEPLOY_USER}@${STAGING_HOST}:/tmp/myproject-release.tar.gz

                        ssh -o StrictHostKeyChecking=accept-new \
                            ${DEPLOY_USER}@${STAGING_HOST} 'bash -s' <<'REMOTE'

                        set -e

                        APP_DIR="/var/www/myproject"

                        mkdir -p "$APP_DIR"

                        tar -xzf /tmp/myproject-release.tar.gz -C "$APP_DIR"

                        rm -f /tmp/myproject-release.tar.gz

                        cd "$APP_DIR"

                        if [ ! -x venv/bin/python ]; then
                            python3 -m venv venv
                        fi

                        venv/bin/pip install --upgrade pip

                        venv/bin/pip install -r requirements.txt

                        set -a
                        source /etc/myproject.env
                        set +a

                        venv/bin/python manage.py collectstatic --noinput

                        sudo -n /usr/bin/systemctl restart gunicorn

                        sudo -n /usr/bin/systemctl is-active --quiet gunicorn

                        echo "Staging Deployment Successful"

REMOTE
                    '''
                }
            }
        }

        stage('Validate Production Version') {

            when {
                expression {
                    return params.DEPLOY_PRODUCTION &&
                           params.PRODUCTION_VERSION?.trim()
                }
            }

            steps {

                script {

                    if (!(params.PRODUCTION_VERSION ==
                       ~ /^v[0-9]+(\.[0-9]+){1,2}([.-][A-Za-z0-9.-]+)?$/)) {

                        error(
                            'Invalid Production Version. Example: v1.0.0'
                        )
                    }
                }

                sh '''
                    set -e

                    git fetch --force --tags origin

                    git rev-parse --verify \
                        "refs/tags/${PRODUCTION_VERSION}^{commit}"

                    git archive \
                        --format=tar.gz \
                        --output=release-production.tar.gz \
                        "${PRODUCTION_VERSION}"
                '''

                archiveArtifacts artifacts: 'release-production.tar.gz'

                stash(
                    name: 'production-release',
                    includes: 'release-production.tar.gz'
                )
            }
        }

        stage('Production Approval') {

            when {
                expression {
                    return params.DEPLOY_PRODUCTION &&
                           params.PRODUCTION_VERSION?.trim()
                }
            }

            steps {

                script {

                    def approvedBy = input(
                        message: """
                        PRODUCTION DEPLOYMENT APPROVAL

                        Server: ${env.PRODUCTION_HOST}

                        Version: ${params.PRODUCTION_VERSION}

                        Do you approve this deployment?
                        """,

                        ok: 'Approve Production Deployment',

                        submitterParameter: 'APPROVED_BY'
                    )

                    echo "Production Approved By: ${approvedBy}"
                }
            }
        }

        stage('Deploy Production') {

            when {
                expression {
                    return params.DEPLOY_PRODUCTION &&
                           params.PRODUCTION_VERSION?.trim()
                }
            }

            steps {

                unstash 'production-release'

                sshagent(credentials: [env.PRODUCTION_CREDENTIAL]) {

                    sh '''
                        set -e

                        echo "Deploying to Production Server"

                        scp -o StrictHostKeyChecking=accept-new \
                            release-production.tar.gz \
                            ${DEPLOY_USER}@${PRODUCTION_HOST}:/tmp/myproject-release.tar.gz

                        ssh -o StrictHostKeyChecking=accept-new \
                            ${DEPLOY_USER}@${PRODUCTION_HOST} 'bash -s' <<'REMOTE'

                        set -e

                        APP_DIR="/var/www/myproject"

                        mkdir -p "$APP_DIR"

                        tar -xzf /tmp/myproject-release.tar.gz -C "$APP_DIR"

                        rm -f /tmp/myproject-release.tar.gz

                        cd "$APP_DIR"

                        if [ ! -x venv/bin/python ]; then
                            python3 -m venv venv
                        fi

                        venv/bin/pip install --upgrade pip

                        venv/bin/pip install -r requirements.txt

                        set -a
                        source /etc/myproject.env
                        set +a

                        venv/bin/python manage.py collectstatic --noinput

                        sudo -n /usr/bin/systemctl restart gunicorn

                        sudo -n /usr/bin/systemctl is-active --quiet gunicorn

                        echo "Production Deployment Successful"

REMOTE
                    '''
                }
            }
        }

    }

    post {

        success {
            echo 'CI/CD Pipeline Completed Successfully'
        }

        failure {
            echo 'CI/CD Pipeline Failed. Please check Jenkins Console Output.'
        }

        aborted {
            echo 'CI/CD Pipeline Aborted'
        }
    }
}
