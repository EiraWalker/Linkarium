class LibraryRouter:
    def db_for_read(self, model, **hints):
        return "library" if model._meta.app_label == "library" else "default"

    db_for_write = db_for_read

    def allow_relation(self, obj1, obj2, **hints):
        return (obj1._meta.app_label == "library") == (obj2._meta.app_label == "library")

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        return db == ("library" if app_label == "library" else "default")
