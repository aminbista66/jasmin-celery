#!/bin/bash

# Specify the variable name and new value
variable_name="old_http_uri"
new_value="http://jasmin:1401"

# Replace the value in the Python script
sed -i "s/$variable_name = .*/$variable_name = \"$new_value\"/" /build/jasmin/protocols/rest/config.py
