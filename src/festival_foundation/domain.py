"""定义基础服务允许登记的资料类别。"""

ALLOWED_CATEGORIES = frozenset({
    "organization_profile",
    "site_registry",
    "duty_resource",
    "actor_assignment",
})


def is_allowed_category(value: str) -> bool:
    return value in ALLOWED_CATEGORIES
