from dojo.product_type.api.views import (
    ProductTypeGroupViewSet,
    ProductTypeMemberViewSet,
    ProductTypeViewSet,
)


def add_product_type_urls(router):
    router.register("product_types", ProductTypeViewSet, basename="product_type")
    # Route / basename pairs restored verbatim from the pre-3.0 registrations
    # (db1932c9e:dojo/urls.py), matching the removal comment in dojo/urls.py.
    router.register("product_type_members", ProductTypeMemberViewSet, basename="product_type_member")
    router.register("product_type_groups", ProductTypeGroupViewSet, basename="product_type_group")
    return router
