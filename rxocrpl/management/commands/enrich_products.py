from django.core.management.base import BaseCommand
from rxocrpl.models import Product
from rxocrpl.enrichment_service import sync_product_from_external_sources, update_product_from_ndc_entry
from rxocrpl.ocr.ndc_directory import get_directory
from rxocrpl.updates import update_product_labelers

class Command(BaseCommand):
    help = 'Bulk-enrich products in the database from FDA and RxNorm'

    def add_arguments(self, parser):
        parser.add_argument('--ndc', type=str, help='Enrich a specific NDC')
        parser.add_argument('--all', action='store_true', help='Enrich all products in the database')
        parser.add_argument('--local', action='store_true', help='Use local FDA CSV instead of online API')
        parser.add_argument('--labelers', action='store_true', help='Update labelers from local database')
        parser.add_argument('--skip-enriched', action='store_true', help='Skip products that already have RxCUI mapping')
        parser.add_argument('--limit', type=int, help='Limit the number of products to process')

    def handle(self, *args, **options):
        if options['labelers']:
            self.stdout.write("Updating labelers...")
            update_product_labelers()
            self.stdout.write(self.style.SUCCESS("Successfully updated labelers"))

        if options['ndc']:
            self.stdout.write(f"Enriching product {options['ndc']}...")
            sync_product_from_external_sources(options['ndc'])
            self.stdout.write(self.style.SUCCESS(f"Successfully enriched {options['ndc']}"))
        elif options['all']:
            products = Product.objects.all()
            if options['skip_enriched']:
                products = products.filter(rxcui_mapping={})
            
            if options['limit']:
                products = products[:options['limit']]
            
            total = products.count()
            self.stdout.write(f"Enriching {total} products...")
            
            local_dir = None
            if options['local']:
                self.stdout.write("Loading local NDC directory...")
                local_dir = get_directory()
                local_dir.load()

            for i, product in enumerate(products, 1):
                self.stdout.write(f"[{i}/{total}] Syncing {product.product_ndc}...")
                try:
                    if options['local'] and local_dir:
                        # Use local data for FDA part
                        entry = local_dir.lookup_ndc(product.product_ndc)
                        if not entry:
                            # Try direct lookup since product_ndc is already product-level
                            entry = local_dir._by_ndc.get(product.product_ndc)
                        
                        if entry:
                            update_product_from_ndc_entry(product, entry)
                        else:
                            self.stdout.write(self.style.WARNING(f"NDC {product.product_ndc} not found in local directory"))
                        
                        # We still need RxNorm online if we want full enrichment
                        # sync_product_from_external_sources(product.product_ndc) # This would repeat FDA call
                        # For now, let's just do FDA local. Full enrichment can follow.
                    else:
                        sync_product_from_external_sources(product.product_ndc)
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"Failed to enrich {product.product_ndc}: {e}"))
            self.stdout.write(self.style.SUCCESS(f"Successfully enriched {total} products"))
        elif not options['labelers']:
            self.stdout.write(self.style.ERROR("Please specify --ndc, --all, or --labelers"))
