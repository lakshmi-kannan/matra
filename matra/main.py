import sys
import os
import yaml
import argparse

def _parse_args():
    parser = argparse.ArgumentParser(description='Spin up pymatra')
    parser.add_argument('-c', '--config', type=str, help='Config file to use')
    parser.parser_args()

    if not args.config:
        print(parser.usage)
        raise Exception('Valid config file required')

    config_parser = ConfigParser(args.config)
    return config_parser.parse()

def main():
    try:
        parsed_config = _parse_args()
    except, e:
        sys.exit(1)

   
main()
