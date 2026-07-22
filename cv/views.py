# import json
#
# from django.contrib.auth.decorators import login_required
# from django.http import JsonResponse
# from django.shortcuts import render
# from django.views.decorators.http import require_POST
#
# from .models import ComponentLibraryEntry, CompoundedSterileProduct, ComponentInstance
#
#
# def csp_builder(request):
#     """CSP Order Builder page — pick components, see live volume & concentration totals."""
#     library_entries = ComponentLibraryEntry.objects.select_related("manufacturer").all()
#     entries_json = []
#     for entry in library_entries:
#         data = entry.data or {}
#         entries_json.append({
#             "id": entry.id,
#             "manufacturer": entry.manufacturer.name,
#             "code": entry.code,
#             "drug_name": data.get("drug_name", ""),
#             "strength": data.get("strength", ""),
#             "strength_unit": data.get("strength_unit", ""),
#             "concentration": data.get("concentration", ""),
#             "concentration_unit": data.get("concentration_unit", ""),
#             "volume_ml": data.get("volume_ml", ""),
#             "form": data.get("form", ""),
#             "ndc": data.get("ndc", ""),
#             "label": f"{data.get('drug_name', entry.code)} {data.get('strength', '')} — {entry.manufacturer.name}",
#         })
#     return render(request, "cv/csp_builder.html", {
#         "library_entries_json": json.dumps(entries_json),
#     })
#
#
# @require_POST
# @login_required
# def csp_builder_save(request):
#     """Save a completed CSP order from the builder."""
#     try:
#         payload = json.loads(request.body)
#     except json.JSONDecodeError:
#         return JsonResponse({"error": "Invalid JSON"}, status=400)
#
#     notes = payload.get("notes", "")
#     components = payload.get("components", [])
#     if not components:
#         return JsonResponse({"error": "At least one component is required"}, status=400)
#
#     csp = CompoundedSterileProduct.objects.create(
#         preparer=request.user,
#         notes=notes,
#     )
#     for comp in components:
#         ComponentInstance.objects.create(
#             compound=csp,
#             component_id=comp["library_entry_id"],
#             lot_number=comp.get("lot_number", ""),
#             expiration=comp.get("expiration") or None,
#             volume_ml=comp.get("volume_ml") or None,
#         )
#     return JsonResponse({"id": csp.id, "message": "CSP order saved successfully"})
