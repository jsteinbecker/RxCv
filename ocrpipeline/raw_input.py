"""Raw detection output, parsed from the user's document."""

RAW = {
  "reference_image": "/Users/jts/Documents/ocr-rx3.png",
  "images": [
    "/Users/jts/Documents/ocr-rx3.png",
    "/Users/jts/Documents/ocr-rx4.png"
  ],
  "detections": [
    {"instance_id": 0, "source_image": "/Users/jts/Documents/ocr-rx3.png", "class_label": "bottle", "score": 0.627, "bbox": [606,69,863,592], "mask_area": 112207, "lot": "308777251000", "exp": "05/30/2028", "ndc": None},
    {"instance_id": 1, "source_image": "/Users/jts/Documents/ocr-rx3.png", "class_label": "bottle", "score": 0.609, "bbox": [867,73,1115,594], "mask_area": 109136, "lot": "308777251000", "exp": "05/30/2028", "ndc": None},
    {"instance_id": 2, "source_image": "/Users/jts/Documents/ocr-rx3.png", "class_label": "bottle", "score": 0.571, "bbox": [966,612,1170,1004], "mask_area": 68217, "lot": "HK3092", "exp": "12/26", "ndc": None},
    {"instance_id": 3, "source_image": "/Users/jts/Documents/ocr-rx3.png", "class_label": "bottle", "score": 0.570, "bbox": [740,606,941,1000], "mask_area": 67883, "lot": "HK3092", "exp": "12/26", "ndc": None},
    {"instance_id": 4, "source_image": "/Users/jts/Documents/ocr-rx3.png", "class_label": "bottle", "score": 0.622, "bbox": [1117,78,1360,600], "mask_area": 107605, "lot": "308777251000", "exp": "05/30/2028", "ndc": None},
    {"instance_id": 5, "source_image": "/Users/jts/Documents/ocr-rx3.png", "class_label": "filter", "score": 0.403, "bbox": [13,7,1439,1079], "mask_area": 752830, "lot": "HK3092", "exp": "12/26", "ndc": "0264-7800-10"},
    {"instance_id": 6, "source_image": "/Users/jts/Documents/ocr-rx3.png", "class_label": "vial", "score": 0.358, "bbox": [967,611,1169,1003], "mask_area": 68215, "lot": "HK3092", "exp": "12/26", "ndc": None},
    {"instance_id": 7, "source_image": "/Users/jts/Documents/ocr-rx3.png", "class_label": "iv bag", "score": 0.392, "bbox": [81,49,571,960], "mask_area": 287901, "lot": "2JD588", "exp": "07/27", "ndc": "0264-7800-10"},
    {"instance_id": 8, "source_image": "/Users/jts/Documents/ocr-rx3.png", "class_label": "vial", "score": 0.250, "bbox": [1118,78,1361,600], "mask_area": 107604, "lot": "308777251000", "exp": "05/30/2028", "ndc": None},
    {"instance_id": 9, "source_image": "/Users/jts/Documents/ocr-rx3.png", "class_label": "bag", "score": 0.253, "bbox": [605,68,863,593], "mask_area": 112210, "lot": "308777251000", "exp": "05/30/2028", "ndc": None},
    {"instance_id": 10, "source_image": "/Users/jts/Documents/ocr-rx3.png", "class_label": "vial", "score": 0.346, "bbox": [739,605,941,1000], "mask_area": 67881, "lot": "HK3092", "exp": "12/26", "ndc": None},
    {"instance_id": 11, "source_image": "/Users/jts/Documents/ocr-rx4.png", "class_label": "bottle", "score": 0.631, "bbox": [232,11,533,615], "mask_area": 156209, "lot": None, "exp": None, "ndc": None},
    {"instance_id": 12, "source_image": "/Users/jts/Documents/ocr-rx4.png", "class_label": "bottle", "score": 0.634, "bbox": [875,12,1175,615], "mask_area": 154085, "lot": None, "exp": None, "ndc": None},
    {"instance_id": 13, "source_image": "/Users/jts/Documents/ocr-rx4.png", "class_label": "bottle", "score": 0.628, "bbox": [556,14,855,614], "mask_area": 155594, "lot": None, "exp": None, "ndc": "0009-0224-20"},
    {"instance_id": 14, "source_image": "/Users/jts/Documents/ocr-rx4.png", "class_label": "bottle", "score": 0.552, "bbox": [729,614,974,779], "mask_area": 30142, "lot": None, "exp": None, "ndc": None},
    {"instance_id": 15, "source_image": "/Users/jts/Documents/ocr-rx4.png", "class_label": "bottle", "score": 0.550, "bbox": [419,615,667,779], "mask_area": 30218, "lot": None, "exp": None, "ndc": None},
    {"instance_id": 16, "source_image": "/Users/jts/Documents/ocr-rx4.png", "class_label": "vial", "score": 0.268, "bbox": [421,614,667,780], "mask_area": 30218, "lot": None, "exp": None, "ndc": None},
    {"instance_id": 17, "source_image": "/Users/jts/Documents/ocr-rx4.png", "class_label": "filter", "score": 0.320, "bbox": [0,3,1244,780], "mask_area": 424887, "lot": None, "exp": None, "ndc": None},
  ],
  # Image dimensions inferred from the largest bboxes per image
  "image_dims": {
    "/Users/jts/Documents/ocr-rx3.png": (1440, 1080),
    "/Users/jts/Documents/ocr-rx4.png": (1244, 780),
  },
}
