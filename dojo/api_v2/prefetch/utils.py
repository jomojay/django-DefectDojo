from django.db.models.fields import related


def _is_many_to_many_relation(field):
    """
    Check if a field specified a many-to-many relationship as defined by django.
    This is the case if the field is an instance of the ManyToManyDescriptor as generated
    by the django framework

    Args:
        field (django.db.models.fields): The field to check

    Returns:
        bool: true if the field is a many-to-many relationship

    """
    return isinstance(field, related.ManyToManyDescriptor)


def _is_one_to_one_relation(field):
    """
    Check if a field specified a one-to-one relationship as defined by django.
    This is the case if the field is an instance of the ForwardManyToOne as generated
    by the django framework

    Args:
        field (django.db.models.fields): The field to check

    Returns:
        bool: true if the field is a one-to-one relationship

    """
    return isinstance(field, related.ForwardManyToOneDescriptor)


def get_prefetchable_fields(serializer, prefetcher=None):
    """
    Get the fields that ``?prefetch=`` can actually serve for the given serializer.

    Relations are discovered by introspecting the serializer's model, then
    filtered down to the ones ``_Prefetcher`` is able to return. A relation is
    only prefetchable if a serializer is registered for the model on the far
    side of it: ``_Prefetcher._prefetch`` looks one up via ``_find_serializer``
    and skips the field entirely when there is none, so advertising such a
    field would promise a response key that can never appear.

    A model existing does not oblige the API to expose it. RBAC models
    (``Dojo_Group`` and friends) are reachable from ``Product`` /
    ``Product_Type`` as plain model relations but have no serializer yet, so
    they are correctly filtered out here until one is added.

    Args:
        serializer (Serializer): the serializer (class or instance) to introspect
        prefetcher (_Prefetcher, optional): an existing prefetcher to reuse for
            serializer lookups. Built on demand when not supplied.

    """

    def _is_field_prefetchable(field):
        return _is_one_to_one_relation(field) or _is_many_to_many_relation(
            field,
        )

    meta = getattr(serializer, "Meta", None)
    if meta is None:
        return []

    model = getattr(meta, "model", None)
    if model is None:
        return []

    fields = []
    for field_name in dir(model):
        field = getattr(model, field_name)
        if _is_field_prefetchable(field):
            # ManyToMany relationship can be reverse
            if hasattr(field, "reverse") and field.reverse:
                fields.append((field_name, field.field.model))
            else:
                fields.append((field_name, field.field.related_model))

    if prefetcher is None:
        from dojo.api_v2.prefetch.prefetcher import (  # noqa: PLC0415 -- lazy import, avoids circular dependency
            _Prefetcher,
        )

        prefetcher = _Prefetcher()

    return [
        (field_name, field_type)
        for field_name, field_type in fields
        if prefetcher._find_serializer(field_type) is not None
    ]
