def load_class(full_class_name):
    """Load a class from a fully qualified name

     full_class_name should be like 'module.submodule.ClassName
    '"""
    parts = full_class_name.split(".")
    module_name = ".".join(parts[:-1])
    class_name = parts[-1]

    module = __import__(module_name)

    # Navigate through nested modules
    for component in parts[1:-1]:
        module = getattr(module, component)

    return getattr(module, class_name)
