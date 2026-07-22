from __future__ import annotations

from django.core.management.base import BaseCommand

from rxocrpl.dailymed_rxnorm_linker import (
    DEFAULT_MAP_PATH,
    link_product_from_setid_map,
    load_setid_map,
)
from rxocrpl.models import Product


class Command(BaseCommand):
    help = "Link Products to RxNorm Concepts using the DailyMed setid map"

    def add_arguments(self, parser):
        parser.add_argument("--ndc", type=str, help="Link a specific product NDC")
        parser.add_argument("--all", action="store_true", help="Link all products")
        parser.add_argument("--limit", type=int, help="Limit the number of products")
        parser.add_argument(
            "--map-file",
            type=str,
            default=str(DEFAULT_MAP_PATH),
            help="Path to rxnorm-dailymed_setid_map.txt",
        )

    def handle(self, *args, **options):
        if not options["ndc"] and not options["all"]:
            self.stdout.write(self.style.ERROR("Please specify --ndc or --all"))
            return

        setid_index = load_setid_map(options["map_file"])

        if options["ndc"]:
            product = Product.objects.filter(product_ndc=options["ndc"]).first()
            if not product:
                self.stdout.write(
                    self.style.WARNING(f"Product {options['ndc']} was not found")
                )
                return

            entry = link_product_from_setid_map(product, setid_index)
            if entry is None:
                self.stdout.write(
                    self.style.WARNING(
                        f"No DailyMed setid mapping found for {options['ndc']}"
                    )
                )
                return

            self.stdout.write(
                self.style.SUCCESS(
                    f"Linked {options['ndc']} -> {entry.rxcui} ({entry.tty})"
                )
            )
            return

        products = Product.objects.all().order_by("product_ndc")
        if options["limit"]:
            products = products[: options["limit"]]

        linked = 0
        skipped = 0
        total = products.count()
        self.stdout.write(f"Linking {total} products from DailyMed setid map...")

        for index, product in enumerate(products, 1):
            self.stdout.write(f"[{index}/{total}] {product.product_ndc}...", ending="")
            entry = link_product_from_setid_map(product, setid_index)
            if entry is None:
                skipped += 1
                self.stdout.write(" skipped")
                continue

            linked += 1
            self.stdout.write(f" linked to {entry.rxcui} ({entry.tty})")

        self.stdout.write(
            self.style.SUCCESS(
                f"Finished linking products: linked={linked}, skipped={skipped}"
            )
        )
