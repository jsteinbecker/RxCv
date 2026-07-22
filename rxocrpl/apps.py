from django.apps import AppConfig


class RxOcrPlConfig(AppConfig):
      name = 'rxocrpl'

      def ready(self):
            import rxocrpl.signals  # noqa: F401
