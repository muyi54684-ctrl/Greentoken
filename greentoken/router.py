def route(classification, config):
    return config["models"][classification["level"]]
