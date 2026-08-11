from dojo.asset.api.views import (
    AssetAPIScanConfigurationViewSet,
    AssetGroupViewSet,
    AssetMemberViewSet,
    AssetViewSet,
)


def add_asset_urls(router):
    router.register(r"assets", AssetViewSet, basename="asset")
    router.register(r"asset_api_scan_configurations", AssetAPIScanConfigurationViewSet,
                    basename="asset_api_scan_configuration")
    # Route / basename pairs restored verbatim from db1932c9e:dojo/asset/api/urls.py,
    # matching the removal comment these two replace.
    router.register(r"asset_groups", AssetGroupViewSet, basename="asset_group")
    router.register(r"asset_members", AssetMemberViewSet, basename="asset_member")
    return router
