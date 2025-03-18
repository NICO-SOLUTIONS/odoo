import { serializeDateTime } from "@web/core/l10n/dates";

const simpleSerialization = (record, opts, { X2MANY_TYPES, DATE_TIME_TYPE }) => {
    const result = {};
    const ownFields = record.model.fields;
    for (const name in ownFields) {
        const field = ownFields[name];
        if (field.type === "many2one") {
            result[name] = record[name]?.id || record.raw[name] || false;
        } else if (X2MANY_TYPES.has(field.type)) {
            const ids = [...record[name]].map((record) => record.id);
            result[name] = ids.length ? ids : record.raw[name] || [];
        } else if (DATE_TIME_TYPE.has(field.type) && typeof record[name] === "object") {
            result[name] = serializeDateTime(record[name]);
        } else if (typeof record[name] === "object") {
            result[name] = JSON.stringify(record[name]);
        } else {
            result[name] = record[name] !== undefined ? record[name] : false;
        }
    }
    return result;
};

const deepSerialization = (
    record,
    opts,
    {
        DYNAMIC_MODELS,
        X2MANY_TYPES,
        DATE_TIME_TYPE,
        serialized = {},
        uuidMapping = {},
        parentRelInverseName = null,
        stack = [],
    }
) => {
    const result = {};
    const { fields, name: currentModel } = record.model;

    const recursiveSerialize = (childRecord, parentRelInverseName) =>
        deepSerialization(childRecord, opts, {
            DYNAMIC_MODELS,
            X2MANY_TYPES,
            DATE_TIME_TYPE,
            serialized,
            uuidMapping,
            parentRelInverseName,
            stack,
        });

    // We only care about the fields present in python model
    for (const [fieldName, field] of Object.entries(fields)) {
        if (field.local || field.related || field.compute || field.dummy) {
            continue;
        }

        const relatedModel = field.relation;
        const targetModel = field.model;
        const relatedCommands = record.models.commands[relatedModel];
        const modelCommands = record.models.commands[currentModel];

        if (DYNAMIC_MODELS.includes(relatedModel) && !serialized[relatedModel]) {
            serialized[relatedModel] = {};
        }
        if (DYNAMIC_MODELS.includes(currentModel) && !serialized[currentModel]) {
            serialized[currentModel] = { [record.uuid]: record.uuid };
        }
        if (DYNAMIC_MODELS.includes(targetModel) && !uuidMapping[targetModel]) {
            uuidMapping[targetModel] = {};
        }
        if (X2MANY_TYPES.has(field.type) && record[fieldName]) {
            if (DYNAMIC_MODELS.includes(relatedModel)) {
                const toUpdate = [];
                const toCreate = [];

                for (const childRecord of record[fieldName]) {
                    if (serialized[relatedModel][childRecord.uuid]) {
                        continue;
                    }

                    if (
                        typeof childRecord.id === "number" &&
                        relatedCommands.update.has(childRecord.id)
                    ) {
                        toUpdate.push(childRecord);
                        if (opts.clear) {
                            relatedCommands.update.delete(childRecord.id);
                        }
                    } else if (typeof childRecord.id !== "number") {
                        toCreate.push(childRecord);
                    }
                    serialized[relatedModel][childRecord.uuid] = childRecord.uuid;
                }
                // The stack defers processing of x2many relationships to ensure objects are only serialized
                // once in their first encountered parent, preventing redundant serialization.
                stack.push([
                    result,
                    fieldName,
                    () => [
                        ...(result[fieldName] || []),
                        ...toUpdate.map((childRecord) => [
                            1,
                            childRecord.id,
                            recursiveSerialize(childRecord, field.inverse_name),
                        ]),
                        ...toCreate.map((childRecord) => [
                            0,
                            0,
                            recursiveSerialize(childRecord, field.inverse_name),
                        ]),
                    ],
                ]);
            } else {
                result[fieldName] = record[fieldName].map((childRecord) => {
                    if (typeof childRecord.id !== "number") {
                        throw new Error(
                            `Trying to create a non serializable record '${relatedModel}'`
                        );
                    }
                    return childRecord.id;
                });
            }

            if (modelCommands.unlink.has(fieldName) || modelCommands.delete.has(fieldName)) {
                result[fieldName] = result[fieldName] || [];
                const processRecords = (records, cmdCode) => {
                    for (const { id, parentId } of records) {
                        const isAlreadyDeleted = serialized[relatedModel]?.["_deleted_" + id];
                        if (parentId === record.id && !isAlreadyDeleted) {
                            const isCascadeDelete =
                                record.models[relatedModel]?.fields[field.inverse_name]?.ondelete;
                            if (isCascadeDelete) {
                                serialized[relatedModel]["_deleted_" + id] = true;
                            }
                            result[fieldName].push([cmdCode, id]);
                        }
                    }
                };
                processRecords(modelCommands.unlink.get(fieldName) || [], 3);
                processRecords(modelCommands.delete.get(fieldName) || [], 2);

                if (opts.clear) {
                    [modelCommands.unlink, modelCommands.delete].forEach((commands) => {
                        const commandList = commands.get(fieldName) || [];
                        const remainingCommands = commandList.filter(
                            ({ parentId }) => parentId !== record.id
                        );
                        if (remainingCommands.length) {
                            commands.set(fieldName, remainingCommands);
                        } else {
                            commands.delete(fieldName);
                        }
                    });
                }
            }
            continue;
        }

        if (field.type === "many2one") {
            const recordId = record[fieldName]?.id;
            if (DYNAMIC_MODELS.includes(relatedModel) && record[fieldName]) {
                if (
                    fieldName !== parentRelInverseName && //mapping not needed for direct child
                    record.uuid &&
                    serialized[relatedModel][record[fieldName].uuid]
                ) {
                    if (typeof recordId !== "number") {
                        //  mapping is only needed for newly created records
                        uuidMapping[targetModel][record.uuid] ??= {};
                        uuidMapping[targetModel][record.uuid][fieldName] = record[fieldName].uuid;
                    }
                }
                serialized[relatedModel][record[fieldName].uuid] = record[fieldName];
            }
            if (typeof recordId === "number" && recordId >= 0) {
                result[fieldName] = record[fieldName].id;
            } else if (record[fieldName] === undefined) {
                result[fieldName] = false;
            }
            continue;
        }
        if (DATE_TIME_TYPE.has(field.type) && typeof record[fieldName] === "object") {
            result[fieldName] = serializeDateTime(record[fieldName]);
            continue;
        }
        if (fieldName === "id") {
            if (typeof record[fieldName] === "number") {
                result[fieldName] = record[fieldName];
            }
            continue;
        }
        result[fieldName] = record[fieldName] !== undefined ? record[fieldName] : false;
    }

    while (stack.length) {
        const [res, key, getValue] = stack.pop();
        res[key] = getValue();
    }

    // Cleanup: remove empty entries from uuidMapping.
    for (const key in uuidMapping) {
        if (
            uuidMapping[key] &&
            typeof uuidMapping[key] === "object" &&
            Object.keys(uuidMapping[key]).length === 0
        ) {
            delete uuidMapping[key];
        }
    }

    return result;
};

export const recursiveSerialization = (record, opts, { X2MANY_TYPES, DATE_TIME_TYPE }) => {
    if (!opts.orm) {
        return simpleSerialization(record, opts, { X2MANY_TYPES, DATE_TIME_TYPE });
    }
    const uuidMapping = {};
    const result = deepSerialization(record, opts, {
        DYNAMIC_MODELS: record._dynamicModels,
        X2MANY_TYPES,
        DATE_TIME_TYPE,
        uuidMapping,
    });
    if (Object.keys(uuidMapping).length !== 0) {
        result.relations_uuid_mapping = uuidMapping;
    }
    return result;
};
