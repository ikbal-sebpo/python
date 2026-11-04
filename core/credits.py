"""Project credits shown at /credits/ (linked from the footer).

Plain data rather than a model: the list changes once or twice a year, so
an edit is a code change + deploy. Set ``former`` on a contributor who has
since left — the page shows a "Former employee" badge instead of rewriting
their name.
"""

RASHEL = {"name": "S. M. Rashel", "role": "Senior Manager"}

CREDIT_AREAS = [
    {
        "title": "Product Owner",
        "icon": "crown",
        "description": (
            "Project initiation and high-level requirement definition, requirement "
            "finalization, business direction, workflow design feedback, stakeholder "
            "alignment, project approval, and go-live approval."
        ),
        "people": [
            {"name": "Raihanul Islam", "role": "Chief Human Resources Officer"},
            {"name": "Tanveer Mohammad Noman", "role": "Chief Technology Officer"},
        ],
    },
    {
        "title": "Project Management",
        "icon": "calendar-check",
        "description": (
            "Planning, coordination, timeline management and cross-team "
            "communication throughout the project."
        ),
        "people": [RASHEL],
    },
    {
        "title": "System Architecture",
        "icon": "network",
        "description": (
            "Approval flow design, module planning, integration concept and "
            "technical decision support."
        ),
        "people": [RASHEL],
    },
    {
        "title": "Database Design",
        "icon": "database",
        "description": (
            "Schema design, data modeling, relationships and query optimization "
            "across all modules."
        ),
        "people": [RASHEL],
    },
    {
        "title": "Workflow & UI/UX Design",
        "icon": "layout-dashboard",
        "description": (
            "Screen flow planning, user experience design and dashboard layout direction."
        ),
        "people": [RASHEL],
    },
    {
        "title": "Templating",
        "icon": "palette",
        "description": (
            "Visual layout, styling and front-end template design for a consistent "
            "look and feel."
        ),
        "people": [RASHEL],
    },
    {
        "title": "Development",
        "icon": "code",
        "description": (
            "Backend, frontend implementation, workflow logic, module development "
            "and bug fixing."
        ),
        "people": [RASHEL],
    },
    {
        "title": "QA & Testing",
        "icon": "bug",
        "description": (
            "Functional testing, workflow testing, bug reporting, validation and "
            "quality support."
        ),
        "people": [
            {"name": "Eftear Rahman", "role": "Software Engineer"},
            {"name": "Ifte Samul Ohy", "role": "Sr. Software Engineer"},
        ],
    },
    {
        "title": "Documentation",
        "icon": "book-open",
        "description": (
            "User guides, technical and process documentation, and knowledge "
            "transfer material, and their upkeep in the company documentation "
            "repository."
        ),
        "people": [
            RASHEL,  # writes the documentation
            # Files and maintains it in the company documentation repository.
            {"name": "Md. Shamim Shahnewaz", "role": "Project Manager"},
        ],
    },
    {
        "title": "Deployment",
        "icon": "rocket",
        "description": (
            "Deployment preparation, production support, monitoring and issue "
            "fixing after release."
        ),
        "people": [
            RASHEL,
            # Provisioned the production VPS and installed its TLS certificate.
            {"name": "Md. Sabbir Rahman Shuvo", "role": "Manager (NSA, IT)", "former": True},
        ],
    },
]

SPECIAL_THANKS = (
    "All stakeholders, including the IT, Admin, Finance, and Operations teams, "
    "who contributed to Orbis."
)
