from dojo.product.api.views import (
    ProductAPIScanConfigurationViewSet,
    ProductGroupViewSet,
    ProductMemberViewSet,
    ProductViewSet,
)


def add_product_urls(router):
    router.register("products", ProductViewSet, basename="product")
    router.register("product_api_scan_configurations", ProductAPIScanConfigurationViewSet, basename="product_api_scan_configuration")
    # Route / basename pairs restored verbatim from the pre-3.0 registrations
    # (db1932c9e:dojo/urls.py), matching the removal comment in dojo/urls.py.
    router.register("product_groups", ProductGroupViewSet, basename="product_group")
    router.register("product_members", ProductMemberViewSet, basename="product_member")
    return router
