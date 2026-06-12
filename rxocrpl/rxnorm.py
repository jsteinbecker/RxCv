from typing import Literal
import requests
import re
import json
from functools import lru_cache

RXNORM_URL = "https://rxnav.nlm.nih.gov"


class RelatedLevel:
      CONCEPT = "concept"
      DRUG = "drug"
      PRODUCT = "product"


type RelatedLevelType = Literal["concept", "drug", "product"]


@lru_cache(maxsize=128)
def get_rxcui(ndc, relation: RelatedLevelType = RelatedLevel.CONCEPT):
      url = RXNORM_URL + f"/REST/relatedndc.json?ndc={ndc}&relation={relation}"
      print(f"Fetching {url}")

      response = requests.get(url, timeout=10)
      if response.status_code == 200:
            data_root = response.json().get("ndcInfoList")
            if not data_root or not isinstance(data_root, dict):
                  return None, None

            data = data_root.get("ndcInfo")
            if not data or not isinstance(data, list) or len(data) == 0:
                  return None, None

            item = data[0]
            return item.get("rxcui"), item.get("tty")
      elif response.status_code == 404:
            return None, None
      else:
            raise Exception(f"Failed to fetch related NDCs: {response.status_code} - {response.text}")


def get_all_rxcui(ndc):
      if not ndc:
            return {}
      output = dict()
      for relation in ["concept", "drug", "product"]:
            value, value_type = get_rxcui(ndc, relation)
            if value:
                  output[relation] = (value, value_type)
      return output


if __name__ == "__main__":
      print(get_all_rxcui("0641-0497"))
