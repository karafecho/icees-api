from sqlalchemy import Table, Column, Integer, String, MetaData, create_engine, func, Sequence, between
from sqlalchemy.sql import select
import json
import os
from .features import features, lookUpFeatureClass
import numpy as np
from .model import get_ids_by_feature, select_associations_to_all_features, select_feature_matrix
from .features import features_dict
import datetime
from utils import to_qualifiers
import traceback
import itertools
import inflection
from .identifiers import get_identifiers, get_features_by_identifier
from functools import reduce, partial
from tx.functional.either import Left, Right
from tx.functional.maybe import maybe, Nothing, Just
import re
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

schema = {
        "population_of_individual_organisms": {
            "chemical_substance": ["association"],
            "disease": ["association"],
            "phenotypic_feature": ["association"],
            "disease_or_phenotypic_feature": ["association"],
            "chemical_substance": ["association"],
            "environment": ["association"],
            "activity_and_behavior": ["association"],
            "drug": ["association"],
            "named_thing": ["association"]
        }
    }

subtypes = {
    "chemical_substance": ["drug"],
    "disease_or_phenotypic_feature": ["disease", "phenotypic_feature"],
    "named_thing": ["chemical_substance", "disease_or_phenotypeic_feature", "environment"]
}

edge_id_map = {}


def closure_subtype(node_type):
    return reduce(lambda x, y : x + y, map(closure_subtype, subtypes.get(node_type, [])), [node_type])


def name_to_ids(table, filter_regex, node_name):
    return list(dict.fromkeys(filter(lambda x: re.match(filter_regex, x), get_identifiers(table, node_name, True))))

def gen_edge_id(cohort_id, node_name, node_id):
    return cohort_id + "_" + node_name + "_" + node_id

def result(source_id, source_node_id, edge_id, target_id, table, filter_regex, feature_property):
    node_name = feature_property["feature_b"]["feature_name"]
    node_ids = name_to_ids(table, filter_regex, node_name)
    def result2(node_id):
        return {
            "node_bindings" : {
                source_id: source_node_id,
                target_id: node_id
            },
            "edge_bindings" : {
                edge_id: [gen_edge_id(source_node_id, node_name, node_id)]
            },
            "score": feature_property["p_value"],
            "score_name": "p value"
        }
    return list(map(result2, node_ids))

def knowledge_graph_node(table, filter_regex, feature_property):
    node_name = feature_property["feature_b"]["feature_name"]
    node_ids = name_to_ids(table, filter_regex, node_name)
    def knowledge_graph_node2(node_id):
        return {
            "name": node_name,
            "id": node_id,
            "type": feature_property["feature_b"]["biolink_class"]
        }
    return list(map(knowledge_graph_node2, node_ids))

def knowledge_graph_edge(source_id, table, filter_regex, feature_property):
    node_name = feature_property["feature_b"]["feature_name"]
    node_ids = name_to_ids(table, filter_regex, node_name)
    edge_name = "association"
    def knowledge_graph_edge2(node_id):
        return {
            "type": edge_name,
            "id": gen_edge_id(source_id, node_name, node_id),
            "source_id": source_id,
            "target_id": node_id,
            "edge_attributes": feature_property
        }
    return list(map(knowledge_graph_edge2, node_ids))

def get(conn, query):
    try:
        message = query.get("message", query)
        cohort_id, table, year, cohort_features, size = message_cohort(conn, message)
        maximum_p_value = message["query_options"].get("maximum_p_value", MAX_P_VAL_DEFAULT)
        filter_regex = message["query_options"].get("regex", ".*")
        feature = to_qualifiers(message["query_options"]["feature"])

        query_graph = query.get("query_graph", query.get("machine_question"))

        nodes = query_graph["nodes"]
        edges = query_graph["edges"]

        if len(nodes) != 2:
            raise NotImplementedError("Number of nodes in query graph must be 2")

        if len(edges) != 1:
            raise NotImplementedError("Number of edges in query graph must be 1")

        nodes_dict = {node_get_id(node): node for node in nodes}
        [edge] = edges

        source_id = edge["source_id"]
        source_node_type = nodes_dict[source_id]["type"]
        if source_node_type not in schema:
            raise NotImplementedError("Sounce node must be one of " + str(schema.keys()))

        target_id = edge["target_id"]
        target_node_type = nodes_dict[target_id]["type"]
        supported_node_types = schema[source_node_type]
        if target_node_type not in supported_node_types:
            raise NotImplementedError("Target node must be one of " + str(supported_node_types.keys()))

        supported_edge_types = supported_node_types[target_node_type]
        edge_id = edge_get_id(edge)
        edge_type = edge["type"]
        if edge_type not in supported_edge_types:
            raise NotImplementedError("Edge must be one of " + str(supported_edge_types))

        cohort_id, size = get_ids_by_feature(conn, table, year, cohort_features)

        supported_types = closure_subtype(target_node_type)

        feature_list = select_associations_to_all_features(conn, table, year, cohort_id, feature, maximum_p_value, lambda x : inflection.underscore(x[3]) in supported_types)
        logger.info(f"feature_list = {feature_list}")

        knowledge_graph_nodes = [{
            "name": "cohort",
            "id": cohort_id,
            "type": "population_of_individual_organisms"
        }] + list(itertools.chain.from_iterable(map(partial(knowledge_graph_node, table, filter_regex), feature_list)))

        knowledge_graph_edges = list(itertools.chain.from_iterable(map(partial(knowledge_graph_edge, source_id, table, filter_regex), feature_list)))

        knowledge_graph = {
            "nodes": knowledge_graph_nodes,
            "edges": knowledge_graph_edges
        }
        
        message = {
            "reasoner_id": "ICEES",
            "tool_version": "3.0.0",
            "datetime": datetime.datetime.now().strftime("%Y-%m-%D %H:%M:%S"),
            "n_results": sum(map(lambda x : len(list(name_to_ids(table, filter_regex, x["feature_b"]["feature_name"]))), feature_list)),
            "message_code": "OK",
            "code_description": "",
            # "query_graph": query_graph,
            "question_graph": query_graph,
            "knowledge_graph": knowledge_graph,
            # "results": list(map(result, feature_list))
            "answers": list(itertools.chain.from_iterable(map(partial(result, source_id, cohort_id, edge_id, target_id, table, filter_regex), feature_list)))
        }
    except Exception as e:
        traceback.print_exc()
        message = {
            "reasoner_id": "ICEES",
            "tool_version": "3.0.0",
            "datetime": datetime.datetime.now().strftime("%Y-%m-%D %H:%M:%S"),
            "message_code": "Error",
            "code_description": str(e),
        }

    return message


def query_feature(table, feature):
    feature_def = features_dict[table][feature]
    ty = feature_def["type"]
    if ty == "string":
        if "enum" not in feature_def:
            return Left("node has type string but has no enum")
        else:
            return Right({
                "feature_name": feature,
                "feature_qualifiers": [{
                    "operator":"=",
                    "value":v
                } for v in feature_def["enum"]]
            })
    elif ty == "integer":
        if "maximum" not in feature_def or "minimum" not in feature_def:
            return Left("node has type integer but has no maximum or has no minimum")
        else:
            return Right({
                "feature_name": feature,
                "feature_qualifiers": [{
                    "operator":"=",
                    "value":v
                } for v in range(feature_def["minimum"], feature_def["maximum"]+1)]
            })
    else:
        return Left(f"unsupported node type {ty}")

    
def co_occurrence_feature_edge(conn, table, year, cohort_features, src_feature, tgt_feature):
    return (
        query_feature(table, src_feature)
        .bind(lambda src_query_feature: (
            query_feature(table, tgt_feature)
            .map(lambda tgt_query_feature: (
                select_feature_matrix(conn, table, year, cohort_features, src_query_feature, tgt_query_feature)["p_value"]
            ))
        ))
    )


def icees_identifiers(table, node):
    return (
        maybe.from_python(node.get("curie"))
        .rec(Right, Left(f"no curie specified at node {node}"))
        .bind(partial(get_features_by_identifier, table))
    )


def co_occurrence_edge(conn, table, year, cohort_features, src_node, tgt_node):
    def handle_src_and_tgt_features(src_features, tgt_features):
        edge_property_value = []
        for src_feature in src_features:
            for tgt_feature in tgt_features:
                edge = co_occurrence_feature_edge(conn, table, year, cohort_features, src_feature, tgt_feature)
                if isinstance(edge, Right):
                    edge_property_value.append({
                        "src_feature": src_feature,
                        "tgt_feature": tgt_feature,
                        "p_value": edge.value
                    })
                else:
                    return edge
        if len(edge_property_value) == 0:
            return Left("no edge found")
        else:
            return Right(edge_property_value)
                    
    return (
        icees_identifiers(table, src_node)
        .bind(lambda src_features: (
            icees_identifiers(table, tgt_node)
            .bind(lambda tgt_features: (
                handle_src_and_tgt_features(src_features, tgt_features)  
            ))
        ))
    )
            

def generate_edge_id(src_node, tgt_node):
    return node_get_id(src_node) + "_" + node_get_id(tgt_node)


def node_get_id(node):
    node_id = node.get("id")
    return node_id if node_id is not None else node.get("node_id")


def edge_get_id(node):
    edge_id = node.get("id")
    return edge_id if edge_id is not None else node.get("edge_id")


def attr(s):
    return lambda d: d[s]

    
def generate_edge(src_node, tgt_node, edge_attributes=None):
    return {
        "id": generate_edge_id(src_node, tgt_node),
        "type": "association",
        "source_id": node_get_id(src_node),
        "target_id": node_get_id(tgt_node),
        **({
           "edge_attributes": edge_attributes
        } if edge_attributes is not None else {})
    }


def convert(attribute_map, qnode):
    return {
        k : k_qnode(qnode) for k, k_qnode in attribute_map.items() if k_qnode in qnode
    }


def convert_qnode_to_node(qnode):
    attribute_map = {
        "id": node_get_id,
        "type": attr("type"),
        "curie": attr("curie")
    }
    return convert(attribute_map, qnode)


def convert_qedge_to_edge(qedge):
    attribute_map = {
        "id": edge_get_id,
        "type": attr("type"),
        "relation": attr("relation"),
        "source_id": attr("source_id"),
        "target_id": attr("target_id"),
        "negated": attr("negated")
    }
    return convert(attribute_map, qedge)
    

def message_cohort(conn, message):
    cohort_definition = message.get("query_options", {})
    cohort_id = cohort_definition.get("cohort_id")
    if cohort_id is None:
        table = cohort_definition.get("table", "patient")
        year = cohort_definition.get("year")
        features = cohort_definition.get("cohort_features", {})
        cohort_id, size = get_ids_by_feature(conn, table, year, features)
    else:
        cohort_definition = get_cohort_definition_by_id(cohort_id)
        if cohort_definition is Nothing:
            raise RuntimeError("cohort with cohort_id not found")
        else:
            table = cohort_definition["table"]
            year = cohort_definition["year"]
            features = cohort_defintion["features"]
            size = cohort_definition["size"]

    return cohort_id, table, year, features, size


MAX_P_VAL_DEFAULT = 1

def co_occurrence_overlay(conn, query):
    try:
        message = query["message"]

        cohort_id, table, year, features, size = message_cohort(conn, message)
        
        query_graph = message.get("knowledge_graph")

        query_nodes = query_graph["nodes"]
        query_edges = query_graph["edges"]

        nodes = list(map(convert_qnode_to_node, query_nodes))
        edges = list(map(convert_qedge_to_edge, query_edges))

        overlay_edges = []
        for src_node in query_nodes:
            for tgt_node in query_nodes:
                edge_attributes = co_occurrence_edge(conn, table, year, features, src_node, tgt_node)
                if isinstance(edge_attributes, Left):
                    return {
                        "reasoner_id": "ICEES",
                        "tool_version": "3.0.0",
                        "datetime": datetime.datetime.now().strftime("%Y-%m-%D %H:%M:%S"),
                        "message_code": "Error",
                        "code_description": edge_attributes.value,
                    }
                else:
                    overlay_edges.append(generate_edge(src_node, tgt_node, edge_attributes=edge_attributes.value))
        knowledge_graph = {
            "nodes": nodes,
            "edges": edges + overlay_edges
        }
        
        message = {
            "reasoner_id": "ICEES",
            "tool_version": "3.0.0",
            "datetime": datetime.datetime.now().strftime("%Y-%m-%D %H:%M:%S"),
            "message_code": "OK",
            "code_description": "",
            "question_graph": query_graph,
            "knowledge_graph": knowledge_graph,
        }
    except Exception as e:
        traceback.print_exc()
        message = {
            "reasoner_id": "ICEES",
            "tool_version": "3.0.0",
            "datetime": datetime.datetime.now().strftime("%Y-%m-%D %H:%M:%S"),
            "message_code": "Error",
            "code_description": str(e),
        }

    return message


def one_hop(conn, query):
    try:
        message = query["message"]
        cohort_id, table, year, cohort_features, size = message_cohort(conn, message)
        maximum_p_value = query.get("query_options", {}).get("maximum_p_value", MAX_P_VAL_DEFAULT)
        filter_regex = query.get("query_options", {}).get("regex", ".*")
        query_graph = message["query_graph"]

        nodes = query_graph["nodes"]
        edges = query_graph["edges"]

        if len(nodes) != 2:
            raise NotImplementedError("Number of nodes in query graph must be 2")

        if len(edges) != 1:
            raise NotImplementedError("Number of edges in query graph must be 1")

        nodes_dict = {node_get_id(node): node for node in nodes}
        [edge] = edges

        source_id = edge["source_id"]
        source_node = nodes_dict[source_id]
        msource_node_identifiers = icees_identifiers(table, source_node)
        if isinstance(msource_node_identifiers, Left):
            raise NotImplementedError(msource_node_identifiers)
        else:
            source_node_identifiers = msource_node_identifiers.value

        target_id = edge["target_id"]
        target_node_type = nodes_dict[target_id]["type"]

        edge_id = edge_get_id(edge)

        feature_set = []
        supported_types = closure_subtype(target_node_type)

        for source_node_identifier in source_node_identifiers:
            feature = query_feature(table, source_node_identifier).value
            ataf = select_associations_to_all_features(conn, table, year, cohort_id, feature, maximum_p_value, feature_set=lambda x : inflection.underscore(x[3]) in supported_types)
            feature_set += ataf

        knowledge_graph_nodes = [convert_qnode_to_node(source_node)] + list(itertools.chain.from_iterable(map(partial(knowledge_graph_node, table, filter_regex), feature_set)))
        
        knowledge_graph_edges = list(itertools.chain.from_iterable(map(partial(knowledge_graph_edge, source_id, table, filter_regex), feature_set)))

        knowledge_graph = {
            "nodes": knowledge_graph_nodes,
            "edges": knowledge_graph_edges
        }
        
        message = {
            "reasoner_id": "ICEES",
            "tool_version": "3.0.0",
            "datetime": datetime.datetime.now().strftime("%Y-%m-%D %H:%M:%S"),
            "n_results": sum(map(lambda x : len(list(name_to_ids(table, filter_regex, x["feature_b"]["feature_name"]))), feature_set)),
            "message_code": "OK",
            "code_description": "",
            # "query_graph": query_graph,
            "question_graph": query_graph,
            "knowledge_graph": knowledge_graph,
            # "results": list(map(result, feature_list))
            "answers": list(itertools.chain.from_iterable(map(partial(result, source_id, source_id, edge_id, target_id, table, filter_regex), feature_set)))
        }
    except Exception as e:
        traceback.print_exc()
        message = {
            "reasoner_id": "ICEES",
            "tool_version": "3.0.0",
            "datetime": datetime.datetime.now().strftime("%Y-%m-%D %H:%M:%S"),
            "message_code": "Error",
            "code_description": traceback.format_exc(),
        }

    return message


def get_schema():
    return schema
