from ringleader.analysis import store

FACTS_PINS = {
    2: "6861bef943910b251d04ae8b49616e7b538e5e8f4d7c0de84245672b10129b6e",
    3: "fa6a8fb04c8cfcaaed3af03f0d94b4f33258f7a3eef1187967c1f427cf097d11",
    4: "a9418573238d610176b62cdd5f938c61017b5b671f7a6d64f09036d52b264b3e",
    5: "071590b0d9c2c0b8a38e06e77b47964b50350d44df70ab14c565ee61c8dfa660",
    6: "5d83ab8bbb633fc948f50cf69430d85e348d0c06cb576e35c8cc6e2d30de0f37",
    7: "f85dabb5362c65da241682348e3e3a0baf4715fda6370e5c69e0c70faf1c5b12",
    8: "f85dabb5362c65da241682348e3e3a0baf4715fda6370e5c69e0c70faf1c5b12",
}
DERIVE_PINS = {
    2: "28e68778cf5c531108abe22e722ad6d9c75e8d52b931526a731e2da0b2516c7f",
    3: "edad9e009d94a478973423a846f59a02d5880f138c39f5edfdab6a224e677f09",
    4: "4f997c3767d635fe3e4c769a24d51f7e21312081a684b430f6b4697c26f82388",
    5: "3b06292536590975c05f5b8d4f4db15b2c471a08abe5e7645ebe12614a4ac2dc",
    6: "504ca18c7ed8df43a9034babc7cfdd4850e43018d1ccbafd2577f1b14631bf07",
}

LABELS_PINS = {
    1: "634b1c7687fb47b20cf9e327ad79f17daeb8e8ce95ed71d6a14be73032cbcc54",
}


def test_facts_fingerprint_is_pinned_to_the_facts_version():
    assert store.facts_fingerprint() == FACTS_PINS[store.FACTS_VERSION]


def test_derive_fingerprint_is_pinned_to_the_derive_version():
    assert store.derive_fingerprint() == DERIVE_PINS[store.DERIVE_VERSION]


def test_labels_ddl_is_pinned_to_the_labels_schema_version():
    assert store.labels_fingerprint() == LABELS_PINS[store.LABELS_SCHEMA_VERSION]
