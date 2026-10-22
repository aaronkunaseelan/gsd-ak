"""Contract weighted-token unit shared by manifests and run receipts."""


def weighted_tokens(usage):
    return (usage.get('input_tokens', 0) + usage.get('output_tokens', 0)
            + 1.25 * usage.get('cache_creation_input_tokens', 0)
            + 0.1 * usage.get('cache_read_input_tokens', 0))
